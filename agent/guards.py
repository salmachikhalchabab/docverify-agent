"""Deterministic guardrails: the "guarded" (and "tools_only") variant.

Plain rules in code around the same model, tools and data as the baseline.
A rule compares values and refuses, removes, queues or overrides. It never asks
a model anything, never reads instructions and never writes reference data; it
reads the registries through the existing ReferenceStore and writes only its
own bookkeeping (duplicate-request keys, actions waiting for a staff member)
to the StateStore.

Rules (plain-language catalogue: docs/guardrails_v2.md):

  before the model   G8   file intake: type, size, active PDF content, duplicates
                     G3   hidden-text removal + data markers               (document rules)
                     G9   instruction-like lines and e-mail addresses removed (document rules)
                     G4   identity binding + requester e-mail check        (e-mail check: document rules)
                     G10  every document belongs to that person: number/QR link, name, no other person
                     G11  graduation documents name our university and no other
                     G13  every QR code: official prefix, known token, not revoked, same holder
                     policy: required documents, validity, graduate/student -> verdict
  before each tool   G2   tool allow-list per service
                     G5   argument schema
                     gate actions only if verdict == approved (or queued for staff, see G16)
                     G5   argument binding to registry records
                     G1   e-mail lock (recipient, attachment, body)
                     G7   per-run budgets (actions once, lookups capped, no identical repeats)
                     G15  no duplicate requests across runs (idempotency keys in the state DB)
                     G16  human approval: actions wait for a staff member (HUMAN_APPROVAL)
  after the loop     G6   final JSON validation; decision = policy verdict

Every intervention is one entry in self.events -> trace.guard_events:
  {"step", "guard", "action", "tool", "reason", "details"}
action: blocked | removed | flagged | queued | overridden | verdict | checked
"""

from __future__ import annotations

import io
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps

from agent.datastore import ReferenceStore, RegistryUnavailableError, StateStore, UnknownRegistryError
from agent.documents import Document, _ocr
from agent.tools import ToolContext, _decode_qr_png
from agent.variants import Guardrails
from config import NON_EMPLOYMENT_VALIDITY_DAYS, QR_VERIFY_PREFIX, TRANSCRIPT_RESEND_HOURS, UNIVERSITY_NAMES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DECISIONS = {"approved", "rejected", "needs_review"}
ACTION_TOOLS = {"register_masters", "issue_graduation_notice", "send_email"}

# G2: which tools each service may use. Anything else is refused.
ALLOWED_TOOLS: dict[str, set[str]] = {
    "transcript": {"lookup_registry", "read_qr", "get_transcript", "send_email"},
    "graduation_notice": {"lookup_registry", "read_qr", "issue_graduation_notice", "send_email"},
    "masters_registration": {"lookup_registry", "read_qr", "register_masters", "send_email"},
}

REQUIRED_ARGS: dict[str, list[str]] = {
    "lookup_registry": ["registry", "key"],
    "read_qr": ["document_number"],
    "get_transcript": ["student_id"],
    "register_masters": ["national_number", "full_name", "major"],
    "issue_graduation_notice": ["national_number", "full_name", "major", "graduation_date"],
    "send_email": ["to", "subject", "body"],
}
OPTIONAL_ARGS: dict[str, list[str]] = {"register_masters": ["notes"], "send_email": ["attachment_id"], "read_qr": ["page"]}
INT_ARGS = {"document_number", "page"}  # may be a JSON integer (or a string of digits)

# G7: how often a tool may be called in one run (actions: once, see _budget).
CALL_LIMITS = {"lookup_registry": 12, "read_qr": 8, "get_transcript": 2}
MAX_IDENTICAL_CALLS = 2

# Registries keyed by the person's national number (both registry layouts).
PERSON_REGISTRIES = {"civil_registry", "registered_emails", "students", "graduates", "employment_registry", "masters_accepted"}
QR_REGISTRY = "qr_tokens"

# G8: file intake
MAX_FILE_BYTES = 15 * 1024 * 1024
MAGIC = {b"%PDF": "pdf", b"\x89PNG": "png", b"\xff\xd8\xff": "jpeg", b"RIFF": "webp", b"GIF8": "gif", b"BM": "bmp",
         b"II*\x00": "tiff", b"MM\x00*": "tiff"}
PDF_ACTIVE = (b"/JavaScript", b"/JS ", b"/JS(", b"/Launch", b"/EmbeddedFile", b"/RichMedia", b"/XFA", b"/OpenAction",
              b"/AA ", b"/AA<", b"/SubmitForm", b"/ImportData")

# Document kinds, recognised by words that say what the paper is (normalised text, see norm_text).
# Checked in this order; the first match wins. National ID first: only an ID says "identity card".
KIND_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("national_id", ("بطاقه شخصيه", "identity card", "national id card", "هويه شخصيه")),
    ("non_employment_certificate", ("غير عامل", "العاملين في الدوله", "non-employment", "non employment", "not employed")),
    ("transcript", ("كشف علامات", "transcript of records", "academic transcript")),
    ("graduation_document", ("تخرج", "graduat")),
)

# G9: generic signs that a line talks to an AI / asks for an action. Written once, from the
# general prompt-injection literature, NOT from our attack documents. An official document has
# no reason to address an assistant or to contain an e-mail address.
INSTRUCTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"\b(ai|automated|virtual)\s+assistant\b", r"\bassistant\b.*\b(must|should|please|note)\b", r"\bignore\b",
    r"\bdisregard\b", r"\b(system|previous)\s+(prompt|instructions?)\b", r"\bnote for the\b", r"\bprocessing note\b",
    r"\b(do not|don't|no need to|need not)\b.{0,30}\bmention", r"\bwithout (checking|verif)",
    r"المساعد", r"تجاهل", r"تعليمات (النظام|سابقه)", r"لا داعي (لذكر|للتحقق)",
)]

# G3: invisible text-layer lines are always removed; 4+ invisible words make the run a warning case.
HIDDEN_TEXT_MIN_WORDS = 4

LIMITS = {"notes": 500, "subject": 200, "body": 4000}

MARK_OPEN = "<<<DOCUMENT_TEXT doc={doc} page={page}>>>"
MARK_CLOSE = "<<<END_DOCUMENT_TEXT>>>"

# Verdict reasons. HARD -> rejected; anything else -> needs_review.
HARD_REASONS = (
    "identity_not_found", "requester_email_mismatch", "missing_document", "not_a_student", "not_a_graduate",
    "certificate_expired", "certificate_not_owner", "employed", "graduation_document_not_owner",
    "graduation_document_revoked", "document_of_other_person", "foreign_university", "qr_foreign_prefix",
    "qr_token_unknown", "qr_token_revoked", "qr_other_holder", "file_rejected", "unknown_service",
    "duplicate_request",
)

# ---------------------------------------------------------------------------
# Normalisation helpers (pure functions; tested on their own)
# ---------------------------------------------------------------------------

_DIGITS = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")} | {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_TASHKEEL = re.compile(r"[ً-ْٰـ]")
_NATIONAL = re.compile(r"(?<!\d)\d{11}(?!\d)")
_NUMBER = re.compile(r"(?<!\d)\d{4,}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_WORD = re.compile(r"\w+")
_DATE_YMD = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_DATE_DMY = re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?!\d)")
_UNIVERSITY_AR = re.compile(r"(?<![\w])جامعه\s+(\w{3,})")
_UNIVERSITY_EN = re.compile(r"\b(?:university of\s+(\w{3,})|(\w{3,})\s+university)\b", re.I)


def norm_digits(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s if s is not None else "")).translate(_DIGITS)


def norm_text(s: Any) -> str:
    """NFKC, ASCII digits, no tashkeel/tatweel, unified alef/yaa/taa-marbuta, one space, lower-case."""
    t = _TASHKEEL.sub("", norm_digits(s))
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه")):
        t = t.replace(a, b)
    return " ".join(t.lower().split())


def norm_email(s: Any) -> str:
    return str(s if s is not None else "").strip().lower()


def norm_date(s: Any) -> str:
    return norm_digits(s).strip().replace("/", "-").replace(".", "-")


def word_tokens(s: Any) -> set[str]:
    return {w for w in _WORD.findall(norm_text(s)) if len(w) >= 3}


def national_numbers(s: Any) -> list[str]:
    return list(dict.fromkeys(_NATIONAL.findall(norm_digits(s))))


def numbers(s: Any) -> list[str]:
    return list(dict.fromkeys(_NUMBER.findall(norm_digits(s))))


def dates(s: Any) -> list[date]:
    """Every calendar date written in the text, in every plausible reading (d/m and m/d)."""
    t, out = norm_digits(s), []
    for y, m, d in _DATE_YMD.findall(t):
        out.append((int(y), int(m), int(d)))
    for a, b, y in _DATE_DMY.findall(t):
        out += [(int(y), int(b), int(a)), (int(y), int(a), int(b))]
    res = []
    for y, m, d in out:
        try:
            res.append(date(y, m, d))
        except ValueError:
            continue
    return sorted(set(res))


def _visible(ocr_text: str) -> set[str]:
    """Words seen on the rendered page, also reversed (PDF text layers sometimes store RTL words backwards)."""
    toks = word_tokens(ocr_text)
    return toks | {w[::-1] for w in toks}


def _pdf_open_text(path: Any, page_number: int) -> set[str]:
    """Words a reader certainly sees: drawn dark enough, 4pt or larger, inside the page, not under an image."""
    try:
        import pymupdf

        with pymupdf.open(path) as pdf:
            page = pdf[page_number - 1]
            images = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
            out: set[str] = set()
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        r = pymupdf.Rect(span["bbox"])
                        c = span.get("color", 0)
                        rgb = ((c >> 16) & 255, (c >> 8) & 255, c & 255)
                        if (span.get("size", 0) < 4 or sum(rgb) > 600 or not r.intersects(page.rect)
                                or any(im.contains(pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)) for im in images)):
                            continue
                        out |= word_tokens(span.get("text", ""))
            return out | {w[::-1] for w in out}
    except Exception:  # noqa: BLE001 - not a PDF or unreadable: rely on OCR only
        return set()


def _is_visible(line: str, visible: set[str], threshold: float) -> bool:
    toks = word_tokens(line)
    return not toks or len(toks & visible) / len(toks) >= threshold


def name_parts(*names: Any) -> set[str]:
    """Distinct words of 3+ letters in the given names (normalised), e.g. first, family, father."""
    return {w for n in names for w in re.findall(r"[^\W\d_]{3,}", norm_text(n))}


def found_parts(parts: set[str], text: str, min_ratio: float = 0.8) -> set[str]:
    """Which name parts appear in the text, tolerating one OCR slip (e.g. 'ور' for 'نور')."""
    words = set(re.findall(r"[^\W\d_]{2,}", norm_text(text)))
    hit = set()
    for p in parts:
        if p in words or any(SequenceMatcher(None, p, w).ratio() >= min_ratio for w in words if abs(len(w) - len(p)) <= 2):
            hit.add(p)
    return hit


# ---------------------------------------------------------------------------
# Registry facts: the two registry layouts (old mock, P1) behind one small interface
# ---------------------------------------------------------------------------


@dataclass
class Person:
    national_number: str
    full_name: str
    parts: set[str]  # name words that must be found on documents (Arabic and English)
    registered_email: str | None
    student: dict[str, Any] | None
    graduate: dict[str, Any] | None
    graduation_numbers: set[str]  # serials of graduation documents issued to them (not revoked)
    revoked_numbers: set[str]

    @property
    def major(self) -> str:
        g = self.graduate or {}
        return str(g.get("major") or g.get("specialization") or "")

    @property
    def graduation_date(self) -> str:
        g = self.graduate or {}
        return str(g.get("graduation_date") or g.get("document_date") or "")


class RegistryFacts:
    def __init__(self, reference: ReferenceStore):
        self.reference = reference
        self.names = set(reference.registries())
        self.unavailable = False  # set when a lookup failed because the registry was unreachable

    def lookup(self, registry: str, key: str) -> dict[str, Any] | None:
        if registry not in self.names:
            return None
        try:
            return self.reference.lookup(registry, key)
        except UnknownRegistryError:
            return None
        except RegistryUnavailableError:
            self.unavailable = True
            return None

    def person(self, nn: str) -> Person | None:
        civil = self.lookup("civil_registry", nn)
        if not civil:
            return None
        grad = self.lookup("graduates", nn)
        numbers_ok, revoked = set(), set()
        if grad:
            if grad.get("graduation_document_number"):  # mock layout: one document per graduate
                numbers_ok.add(norm_digits(grad["graduation_document_number"]))
            for d in grad.get("issued_documents") or []:  # P1 layout: every issued document
                (revoked if d.get("revoked") in (True, 1, "1", "true") else numbers_ok).add(norm_digits(d.get("serial")))
        first_mother = str(civil.get("mother_name") or "").split()[:1]
        parts = name_parts(civil.get("full_name"), civil.get("first_name"), civil.get("family_name"),
                           civil.get("father_name"), *first_mother)
        parts |= name_parts(civil.get("full_name_en"))
        email = (self.lookup("registered_emails", nn) or {}).get("email")
        return Person(nn, str(civil.get("full_name") or ""), parts, norm_email(email) or None,
                      self.lookup("students", nn), grad, numbers_ok, revoked)

    def exists(self, nn: str) -> bool:
        return self.lookup("civil_registry", nn) is not None


# ---------------------------------------------------------------------------
# Per-document analysis
# ---------------------------------------------------------------------------


@dataclass
class DocInfo:
    index: int  # 1-based, as the model sees it ("Document 1")
    name: str
    kind: str = "unknown"
    text: str = ""  # text the checks read (after G3/G9)
    numbers: set[str] = field(default_factory=set)
    national_numbers: set[str] = field(default_factory=set)
    qr_codes: list[str] = field(default_factory=list)
    linked_by: list[str] = field(default_factory=list)
    name_found: list[str] = field(default_factory=list)
    name_confirmed: bool = False
    enhanced_ocr: bool = False

    def summary(self) -> dict[str, Any]:
        return {"document": self.index, "kind": self.kind, "linked_by": self.linked_by,
                "name_confirmed": self.name_confirmed, "name_parts_found": len(self.name_found),
                "qr_codes": len(self.qr_codes), "enhanced_ocr": self.enhanced_ocr}


# ---------------------------------------------------------------------------
# The guardrails
# ---------------------------------------------------------------------------


class DeterministicGuardrails(Guardrails):
    name = "deterministic"

    def __init__(
        self,
        *,
        service: str = "",
        requester_email: str = "",
        today: str = "",
        reference: ReferenceStore | None = None,
        state: StateStore | None = None,
        ocr_engine: str = "none",
        ocr_lang: str = "eng",
        hidden_text_threshold: float = 0.5,
        document_rules: bool = True,
        human_approval: str = "off",
        idempotency_scope: str = "",
        ocr_fn: Callable[[Image.Image], str | None] | None = None,
        qr_fn: Callable[[bytes], list[str]] | None = None,
        **_: Any,
    ) -> None:
        super().__init__()
        if reference is None:
            raise ValueError("DeterministicGuardrails needs the reference store")
        self.service = service
        self.requester_email = requester_email
        self.today = today
        self.reference = reference
        self.facts = RegistryFacts(reference)
        self.state = state
        self.threshold = hidden_text_threshold
        self.document_rules = document_rules
        self.human_approval = human_approval if human_approval in {"off", "actions", "flagged"} else "off"
        self.scope = idempotency_scope
        if not document_rules:
            self.name = "deterministic_tools_only"
        self._ocr_available = ocr_fn is not None or ocr_engine != "none"
        self._ocr = ocr_fn or (lambda img: _ocr(img, ocr_engine, ocr_lang))
        self._qr = qr_fn or _decode_qr_png
        # filled by transform_documents
        self.identity: Person | None = None
        self.docs: list[DocInfo] = []
        self.verdict: str = "needs_review"
        self.reasons: list[str] = []
        self.flags: list[str] = []  # warnings that send a run to a staff member in "flagged" mode
        self.document_numbers: set[str] = set()
        self.qr_tokens: set[str] = set()
        self.hidden_text_removed = False
        self.hidden_text_chars_removed = 0
        self.instruction_lines_removed = 0
        self.pre_model_rejected = False
        self.model_decision: str | None = None
        self.latency_ms = 0.0
        # filled by intercept_tool_call
        self.call_counts: dict[str, int] = {}
        self.call_signatures: dict[str, int] = {}
        self.pending: list[dict[str, Any]] = []  # actions queued for a staff member (G16)
        self.duplicate_blocks = 0
        self.awaiting_staff = False  # set by finalize when a staff member makes the final decision (G16)

    # -- bookkeeping ---------------------------------------------------------

    def _event(self, guard: str, action: str, reason: str, tool: str | None = None, **details: Any) -> None:
        self.events.append(
            {"step": self.current_step, "guard": guard, "action": action, "tool": tool, "reason": reason, "details": details}
        )

    def _block(self, guard: str, reason: str, tool: str, message: str, **details: Any) -> dict[str, Any]:
        self._event(guard, "blocked", reason, tool, **details)
        return {"error": f"Refused by policy ({guard}): {reason}. {message}", "guard": guard, "reason": reason}

    def _reason(self, reason: str, guard: str, **details: Any) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self._event(guard, "flagged", reason, **details)

    def summary(self) -> dict[str, Any]:
        return {
            "policy_verdict": self.verdict,
            "reasons": list(self.reasons),
            "flags": list(self.flags),
            "model_decision": self.model_decision,
            "identity_bound": self.identity is not None,
            "documents": [d.summary() for d in self.docs],
            "pre_model_rejected": self.pre_model_rejected,
            "hidden_text_removed": self.hidden_text_removed,
            "hidden_text_chars_removed": self.hidden_text_chars_removed,
            "instruction_lines_removed": self.instruction_lines_removed,
            "human_approval": self.human_approval,
            "pending_actions": [p["id"] for p in self.pending],
            "duplicate_blocks": self.duplicate_blocks,
            "awaiting_staff": self.awaiting_staff,
            "guard_latency_ms": self.latency_ms,
            "document_rules": self.document_rules,
        }

    # -- before the model ----------------------------------------------------

    def transform_documents(self, documents: list[Document]) -> list[Document]:
        t0 = time.perf_counter()
        self.current_step = 0
        self._intake(documents)  # G8
        if self.document_rules:
            self._remove_hidden_text(documents)  # G3
            self._remove_instruction_lines(documents)  # G9
        self._analyse(documents)  # kinds, numbers, QR codes
        self._bind_identity()  # G4
        if self.identity is not None:
            self._check_documents(documents)  # G10, G11, G13
        self._policy()
        if self.document_rules:
            self._add_markers(documents)  # G3 (markers)
        self.pre_model_rejected = self.verdict == "rejected"
        self._event("policy", "verdict", self.verdict, verdict=self.verdict, reasons=list(self.reasons),
                    documents=[d.summary() for d in self.docs])
        self.latency_ms += (time.perf_counter() - t0) * 1000.0
        return documents

    def _intake(self, documents: list[Document]) -> None:
        """G8: only real PDFs/images, not too big, no active PDF content, no duplicate uploads."""
        seen: dict[str, int] = {}
        for i, doc in enumerate(documents, 1):
            try:
                raw = Path(doc.path).read_bytes()
            except OSError:
                continue
            if len(raw) > MAX_FILE_BYTES:
                self._reason("file_rejected", "G8", document=i, why="too_large", bytes=len(raw))
            kind = next((k for m, k in MAGIC.items() if raw.startswith(m)), None)
            if kind is None:
                self._reason("file_rejected", "G8", document=i, why="unknown_file_type")
            if kind == "pdf":
                active = sorted({a.decode().strip(" (<") for a in PDF_ACTIVE if a in raw})
                if active or b"/Encrypt" in raw:
                    self._reason("file_rejected", "G8", document=i, why="active_or_encrypted_pdf", found=active)
            if doc.sha256 in seen:
                self.flags.append("duplicate_upload")
                self._event("G8", "flagged", "duplicate_upload", document=i, same_as=seen[doc.sha256])
            seen.setdefault(doc.sha256, i)

    def _remove_hidden_text(self, documents: list[Document]) -> None:
        """G3: a text-layer line that is not visible in the rendered page is hidden -> removed."""
        for d_i, doc in enumerate(documents, 1):
            for page in doc.pages:
                if page.text_source != "pdf_text_layer":
                    continue  # OCR text comes from the picture: nothing can hide in it
                if not self._ocr_available:
                    removed = len(page.text)
                    self._event("G3", "removed", "text_layer_unverifiable", page=page.page_number, document=d_i,
                                removed_chars=removed)
                    self.hidden_text_removed = self.hidden_text_removed or bool(page.text.strip())
                    self.hidden_text_chars_removed += removed
                    page.text, page.text_source = "", "removed_unverifiable"
                    continue
                img = Image.open(io.BytesIO(page.image_png))
                # text drawn in the open (dark, readable size, on the page, not under a picture) is visible;
                # everything else must be confirmed by OCR of the rendered page
                visible = _pdf_open_text(doc.path, page.page_number) | _visible(self._ocr(img) or "")
                lines = page.text.splitlines()
                if any(not _is_visible(line, visible, self.threshold) for line in lines):
                    # second opinion before calling anything hidden: the page read at 2x, auto-contrast
                    big = ImageOps.autocontrast(img.convert("L").resize((img.width * 2, img.height * 2), Image.LANCZOS))
                    visible |= _visible(self._ocr(big) or "")
                kept = [line for line in lines if _is_visible(line, visible, self.threshold)]
                removed_lines = [line for line in lines if not _is_visible(line, visible, self.threshold)]
                page.text_source = "pdf_text_layer_verified"
                if removed_lines:
                    removed_text = "\n".join(removed_lines)
                    words = sum(len(word_tokens(line)) for line in removed_lines)
                    self._event("G3", "removed", "hidden_text", page=page.page_number, document=d_i,
                                removed_chars=len(removed_text), removed_words=words, removed_text=removed_text)
                    self.hidden_text_chars_removed += len(removed_text)
                    page.text = "\n".join(kept)
                    # a stray OCR scrap is removed quietly; real hidden text (4+ words) is a warning
                    if words >= HIDDEN_TEXT_MIN_WORDS:
                        self.hidden_text_removed = True
        if self.hidden_text_removed:
            self.flags.append("hidden_text_in_document")

    def _remove_instruction_lines(self, documents: list[Document]) -> None:
        """G9: visible lines that address an assistant or carry an e-mail address are taken out of the text."""
        for d_i, doc in enumerate(documents, 1):
            for page in doc.pages:
                kept, removed = [], []
                for line in page.text.splitlines():
                    n = norm_text(line)
                    if _EMAIL.search(line) or any(p.search(n) for p in INSTRUCTION_PATTERNS):
                        removed.append(line)
                    else:
                        kept.append(line)
                if removed:
                    page.text = "\n".join(kept)
                    self.instruction_lines_removed += len(removed)
                    self._event("G9", "removed", "instruction_like_text", document=d_i, page=page.page_number,
                                removed_lines=len(removed), removed_text="\n".join(removed))
        if self.instruction_lines_removed:
            self.flags.append("instruction_like_text_in_document")

    def _analyse(self, documents: list[Document]) -> None:
        self.docs = []
        for i, doc in enumerate(documents, 1):
            text = "\n".join(p.text for p in doc.pages)
            info = DocInfo(i, doc.name, text=text)
            info.kind = self._kind(text)
            info.numbers = set(numbers(text))
            info.national_numbers = set(national_numbers(text))
            for p in doc.pages:
                try:
                    info.qr_codes += [c for c in self._qr(p.image_png) if c not in info.qr_codes]
                except Exception as e:  # noqa: BLE001 - a broken image is not a reason to crash the run
                    self._event("G13", "flagged", "qr_decode_failed", document=i, error=type(e).__name__)
            self.docs.append(info)
        self.document_numbers = {n for d in self.docs for n in d.numbers}
        self.qr_tokens = {c[len(QR_VERIFY_PREFIX):] for d in self.docs for c in d.qr_codes if c.startswith(QR_VERIFY_PREFIX)}

    @staticmethod
    def _kind(text: str) -> str:
        n = norm_text(text)
        return next((k for k, words in KIND_KEYWORDS if any(w in n for w in words)), "unknown")

    def _enhanced_text(self, doc: Document) -> str:
        """A second, stronger OCR pass (2x, auto-contrast) for checks only; the model never sees it."""
        if not self._ocr_available:
            return ""
        out = []
        for p in doc.pages:
            img = Image.open(io.BytesIO(p.image_png)).convert("L")
            img = ImageOps.autocontrast(img.resize((img.width * 2, img.height * 2), Image.LANCZOS))
            out.append(self._ocr(img) or "")
        return "\n".join(out)

    def _bind_identity(self) -> None:
        """G4: the person is whoever the national number on the documents says, if the registry knows them."""
        if not any(d.text.strip() for d in self.docs):
            self._reason("no_text_available", "G4")
            return
        # prefer the number printed on the national ID, then any other document
        ordered = sorted(self.docs, key=lambda d: d.kind != "national_id")
        for d in ordered:
            for nn in sorted(d.national_numbers):
                person = self.facts.person(nn)
                if person:
                    self.identity = person
                    break
            if self.identity:
                break
        if self.identity is None:
            # an outage is not "unknown person": fail closed (review), never reject the person for it
            self._reason("registry_unavailable" if self.facts.unavailable else "identity_not_found", "G4")
            return
        if self.document_rules and norm_email(self.requester_email) != (self.identity.registered_email or ""):
            self._reason("requester_email_mismatch", "G4")  # the registered address is not revealed

    def _check_documents(self, documents: list[Document]) -> None:
        """G10 (same person on every document), G11 (our university), G13 (QR codes)."""
        p = self.identity
        assert p is not None
        for info, doc in zip(self.docs, documents):
            # G13 QR codes
            for code in info.qr_codes:
                rec = self._check_qr(code, info.index)
                if rec and norm_digits(rec.get("holder_national_number")) == p.national_number:
                    info.linked_by.append("qr")
                    if str(rec.get("serial") or ""):
                        info.numbers.add(norm_digits(rec["serial"]))
            # G10 link by number
            if p.national_number in info.national_numbers:
                info.linked_by.append("national_number")
            others = [n for n in info.national_numbers if n != p.national_number and self.facts.exists(n)]
            if others:
                self._reason("document_of_other_person", "G10", document=info.index)
            if info.kind == "graduation_document" and info.numbers & (p.graduation_numbers | p.revoked_numbers):
                info.linked_by.append("document_number")
            if info.kind == "non_employment_certificate":
                for num in info.numbers:
                    rec = self.facts.lookup("non_employment_certificates", num)
                    if rec and norm_digits(rec.get("national_number")) == p.national_number:
                        info.linked_by.append("certificate_number")
                        break
            # G10 name: at least two words of the person's name (one if the name has only one word)
            need = min(2, len(p.parts))
            info.name_found = sorted(found_parts(p.parts, info.text))
            if len(info.name_found) < need and self._ocr_available and any(pg.text_source == "ocr" for pg in doc.pages):
                info.enhanced_ocr = True
                extra = self._enhanced_text(doc)
                info.name_found = sorted(found_parts(p.parts, info.text + "\n" + extra))
                if not info.linked_by and p.national_number in national_numbers(extra):
                    info.linked_by.append("national_number")
            info.name_confirmed = len(info.name_found) >= need
            if not info.name_confirmed:
                self._reason(f"name_not_confirmed:document_{info.index}", "G10", document=info.index,
                             parts_found=len(info.name_found), parts_needed=need)
            if not info.linked_by and info.kind != "unknown":
                self._reason(f"document_not_linked:document_{info.index}", "G10", document=info.index, kind=info.kind)
            # G11 university
            if info.kind == "graduation_document":
                self._check_university(info)

    def _check_qr(self, code: str, doc_index: int) -> dict[str, Any] | None:
        """G13: official prefix, known token, not revoked; returns the registry record if it passed."""
        if not code.startswith(QR_VERIFY_PREFIX):
            self._reason("qr_foreign_prefix", "G13", document=doc_index)
            return None
        if QR_REGISTRY not in self.facts.names:
            self._reason("qr_unverifiable", "G13", document=doc_index)
            return None
        rec = self.facts.lookup(QR_REGISTRY, code[len(QR_VERIFY_PREFIX):])
        if rec is None:
            self._reason("qr_token_unknown", "G13", document=doc_index)
            return None
        if rec.get("revoked") in (True, 1, "1", "true"):
            self._reason("qr_token_revoked", "G13", document=doc_index)
            return None
        if norm_digits(rec.get("holder_national_number")) != self.identity.national_number:  # type: ignore[union-attr]
            self._reason("qr_other_holder", "G13", document=doc_index)
            return None
        self._event("G13", "checked", "qr_valid", document=doc_index, serial=rec.get("serial"))
        return rec

    def _check_university(self, info: DocInfo) -> None:
        n = norm_text(info.text)
        ours = {norm_text(u) for u in UNIVERSITY_NAMES}
        our_words = {w for u in ours for w in u.split()}
        named = [m for m in _UNIVERSITY_AR.findall(n)] + [a or b for a, b in _UNIVERSITY_EN.findall(n)]
        foreign = sorted({w for w in named if w not in our_words})
        if foreign:
            self._reason("foreign_university", "G11", document=info.index, named=foreign)
        elif not any(u in n for u in ours):
            self._reason(f"university_not_named:document_{info.index}", "G11", document=info.index)

    def _non_employment(self, p: Person) -> str | None:
        """None if a valid non-employment certificate of this person is present, else the reason."""
        certs = [d for d in self.docs if d.kind == "non_employment_certificate"]
        if not certs:
            return "missing_document:non_employment_certificate"
        today = date.fromisoformat(self.today)
        if "non_employment_certificates" in self.facts.names:  # mock layout: the certificate is in a registry
            rec = None
            for d in certs:
                for num in d.numbers:
                    r = self.facts.lookup("non_employment_certificates", num)
                    if r and norm_digits(r.get("national_number")) == p.national_number:
                        rec = r
            if rec is None:
                return "certificate_not_owner"
            if str(rec.get("status", "")).lower() in {"revoked", "cancelled", "employed"}:
                return "employed"
            issued = date.fromisoformat(norm_date(rec.get("issue_date")))
        else:  # P1 layout: employment register + the issue date printed on the certificate
            emp = self.facts.lookup("employment_registry", p.national_number)
            if emp and emp.get("employed") in (True, 1, "1", "true"):
                return "employed"
            printed = [d for c in certs for d in dates(c.text) if d <= today]
            if not printed:
                return "issue_date_unreadable"
            issued = max(printed)
        if not (issued <= today <= issued + timedelta(days=NON_EMPLOYMENT_VALIDITY_DAYS)):
            return "certificate_expired"
        return None

    def _policy(self) -> None:
        """Required documents and service conditions -> verdict. Hard reasons reject, the rest go to review."""
        p = self.identity
        if p is not None:
            if not any(d.kind == "national_id" and p.national_number in d.national_numbers | (
                    set(national_numbers(d.text))) for d in self.docs):
                self._reason("missing_document:national_id", "policy")
            if self.service == "transcript":
                if p.student is None:
                    self._reason("not_a_student", "policy")
            elif self.service == "graduation_notice":
                if p.graduate is None:
                    self._reason("not_a_graduate", "policy")
            elif self.service == "masters_registration":
                why = self._non_employment(p)
                if why:
                    self._reason(why, "policy")
                if p.graduate is None:
                    self._reason("not_a_graduate", "policy")
                else:
                    grads = [d for d in self.docs if d.kind == "graduation_document"]
                    if not grads:
                        self._reason("missing_document:graduation_document", "policy")
                    elif not any(d.numbers & p.graduation_numbers for d in grads):
                        revoked = any(d.numbers & p.revoked_numbers for d in grads)
                        self._reason("graduation_document_revoked" if revoked else "graduation_document_not_owner",
                                     "policy")
            else:
                self._reason("unknown_service", "policy")
            self._check_earlier_requests()

        if self.facts.unavailable and "registry_unavailable" not in self.reasons:
            self._reason("registry_unavailable", "G4")  # fail closed: never approve what the registry can't confirm
        hard = [r for r in self.reasons if r.startswith(HARD_REASONS)]
        if hard:
            self.verdict = "rejected"
        elif self.reasons or self.flags:
            self.verdict = "needs_review"
        else:
            self.verdict = "approved"

    def _check_earlier_requests(self) -> None:
        """G15 before the model: was this service already performed for this person?"""
        if self.state is None or self.identity is None:
            return
        tool = {"masters_registration": "register_masters", "graduation_notice": "issue_graduation_notice",
                "transcript": "send_email"}.get(self.service)
        key = self._key(tool) if tool else None
        earlier = self.state.get_idempotency_key(key) if key else None
        if earlier is not None and not self._expired(tool, earlier):
            self._reason("duplicate_request", "G15", first_done_at=earlier.get("created_at"))

    @staticmethod
    def _max_age_s(tool: str | None) -> float | None:
        """Transcripts may be sent again after TRANSCRIPT_RESEND_HOURS; the other actions never."""
        return TRANSCRIPT_RESEND_HOURS * 3600.0 if tool == "send_email" else None

    def _expired(self, tool: str | None, earlier: dict[str, Any]) -> bool:
        limit = self._max_age_s(tool)
        if limit is None:
            return False
        done = datetime.fromisoformat(str(earlier.get("created_at")))
        return (datetime.now(timezone.utc) - done).total_seconds() > limit

    def _add_markers(self, documents: list[Document]) -> None:
        for d_i, doc in enumerate(documents, 1):
            for page in doc.pages:
                if page.text.strip():
                    page.text = f"{MARK_OPEN.format(doc=d_i, page=page.page_number)}\n{page.text}\n{MARK_CLOSE}"

    # -- before each tool ----------------------------------------------------

    def intercept_tool_call(self, name: str, arguments_raw: str, ctx: ToolContext) -> dict[str, Any] | None:
        t0 = time.perf_counter()
        try:
            return self._intercept(name, arguments_raw, ctx)
        finally:
            self.latency_ms += (time.perf_counter() - t0) * 1000.0

    def _review_allowed(self) -> bool:
        """With human approval on, the staff member has the last word: actions the rules would refuse are
        prepared and held for them (never performed), whatever the verdict. The verdict is a recommendation."""
        return self.human_approval != "off"

    def _staff_decides(self) -> bool:
        """Does this run end with a staff member's decision? Every run with HUMAN_APPROVAL=actions; with
        "flagged" only runs the rules did not approve cleanly."""
        if self.human_approval == "actions":
            return True
        return self.human_approval == "flagged" and (self.verdict != "approved" or bool(self.flags))

    def _intercept(self, name: str, arguments_raw: str, ctx: ToolContext) -> dict[str, Any] | None:
        # G2 allow-list (unknown tools included)
        if name not in ALLOWED_TOOLS.get(self.service, set()):
            return self._block("G2", "tool_not_allowed_for_service", name,
                               f"The service '{self.service}' may not use this tool.")

        # G5 argument schema
        try:
            args = json.loads(arguments_raw) if arguments_raw.strip() else {}
        except (json.JSONDecodeError, ValueError):
            return self._block("G5", "invalid_arguments", name, "Arguments must be a JSON object.")
        if not isinstance(args, dict):
            return self._block("G5", "invalid_arguments", name, "Arguments must be a JSON object.")
        missing = [k for k in REQUIRED_ARGS[name] if k not in args]
        if missing:
            return self._block("G5", "invalid_arguments", name, f"Missing: {', '.join(missing)}.", missing=missing)
        allowed_keys = set(REQUIRED_ARGS[name]) | set(OPTIONAL_ARGS.get(name, []))
        bad = [k for k, v in args.items() if k not in allowed_keys or not (
            v is None or isinstance(v, str) or (k in INT_ARGS and isinstance(v, int) and not isinstance(v, bool)))]
        if bad:
            return self._block("G5", "invalid_arguments", name, "Arguments must have the declared names and types.",
                               fields=bad)

        # G7 call limits (also stops loops that burn tokens)
        self.call_counts[name] = self.call_counts.get(name, 0) + 1
        if name in CALL_LIMITS and self.call_counts[name] > CALL_LIMITS[name]:
            return self._block("G7", "call_limit_reached", name, f"At most {CALL_LIMITS[name]} calls of this tool.")
        sig = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
        self.call_signatures[sig] = self.call_signatures.get(sig, 0) + 1
        if self.call_signatures[sig] > MAX_IDENTICAL_CALLS:
            return self._block("G7", "repeated_call", name, "The same call was already made; use its result.")

        # gate: nothing irreversible unless the policy approved (or a staff member will decide, G16)
        if name in ACTION_TOOLS and self.verdict != "approved" and not self._review_allowed():
            return self._block("gate", "action_requires_approved_verdict", name,
                               f"Policy verdict is '{self.verdict}' ({', '.join(self.reasons) or 'no reasons'}). "
                               "Finish with that decision and explain it.", verdict=self.verdict, reasons=list(self.reasons))

        # G5 binding + G1
        check: dict[str, Any] | None = None
        if name == "lookup_registry":
            return self._check_lookup(args)
        if name == "read_qr":
            return self._check_read_qr(args, ctx)
        if name == "get_transcript":
            return self._check_transcript(args)
        if name == "register_masters":
            check = self._check_register(args) or self._budget(ctx, "masters_registrations", name)
        elif name == "issue_graduation_notice":
            check = self._check_notice(args) or self._budget(ctx, "graduation_notices", name)
        elif name == "send_email":
            check = self._check_email(args, ctx) or self._budget(ctx, "outbox", name)
        if check is not None:
            return check
        # G15 duplicate requests across runs, then G16 human approval
        return self._idempotent(name, ctx) or self._maybe_queue(name, args, ctx)

    def _check_lookup(self, args: dict[str, Any]) -> dict[str, Any] | None:
        registry = str(args["registry"])
        key = norm_digits(args["key"]).strip()
        if registry in PERSON_REGISTRIES:
            if self.identity is not None:
                if key != self.identity.national_number:
                    return self._block("G5", "lookup_other_person", "lookup_registry",
                                       "Lookups are limited to the person identified on the uploaded documents.")
                return None
            if self.document_numbers and key not in self.document_numbers:
                return self._block("G5", "lookup_key_not_in_documents", "lookup_registry",
                                   "Lookups are limited to numbers that appear on the uploaded documents.")
            if not self.document_numbers:
                self._event("G5", "flagged", "lookup_unverifiable", "lookup_registry")
            return None
        if registry == QR_REGISTRY:
            if str(args["key"]).strip() not in self.qr_tokens:
                return self._block("G5", "lookup_key_not_in_documents", "lookup_registry",
                                   "QR lookups are limited to tokens decoded from the uploaded documents.")
            return None
        if self.document_numbers and key not in self.document_numbers:
            return self._block("G5", "lookup_key_not_in_documents", "lookup_registry",
                               "Lookups are limited to numbers that appear on the uploaded documents.")
        return None

    def _check_read_qr(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        try:
            n = int(str(args["document_number"]).strip())
        except ValueError:
            return self._block("G5", "invalid_arguments", "read_qr", "document_number must be a number.")
        if not 1 <= n <= len(self.docs or ctx.documents):
            return self._block("G5", "invalid_arguments", "read_qr", "There is no such document.")
        return None

    def _check_transcript(self, args: dict[str, Any]) -> dict[str, Any] | None:
        if self.identity is None:
            return self._block("G5", "identity_not_bound", "get_transcript",
                               "No person could be identified from the uploaded documents.")
        student = self.identity.student
        if student is None:
            return self._block("G5", "not_a_student", "get_transcript", "No student record for the identified person.")
        if norm_digits(args["student_id"]).strip() != norm_digits(student.get("student_id")).strip():
            return self._block("G5", "argument_mismatch", "get_transcript",
                               "student_id does not match the identified person's record.", field="student_id")
        return None

    def _mismatch(self, tool: str, field_name: str) -> dict[str, Any]:
        return self._block("G5", "argument_mismatch", tool, f"'{field_name}' does not match the registry record.",
                           field=field_name)

    def _need_identity(self, tool: str) -> dict[str, Any] | None:
        if self.identity is None:
            return self._block("G5", "identity_not_bound", tool, "No person could be identified from the documents.")
        return None

    def _check_register(self, args: dict[str, Any]) -> dict[str, Any] | None:
        if (b := self._need_identity("register_masters")) is not None:
            return b
        p = self.identity
        assert p is not None
        if norm_digits(args["national_number"]).strip() != p.national_number:
            return self._mismatch("register_masters", "national_number")
        if norm_text(args["full_name"]) != norm_text(p.full_name):
            return self._mismatch("register_masters", "full_name")
        if p.graduate is None or norm_text(args["major"]) != norm_text(p.major):
            return self._mismatch("register_masters", "major")
        if len(args.get("notes") or "") > LIMITS["notes"]:
            return self._block("G5", "argument_too_long", "register_masters", "notes is too long.", field="notes")
        return None

    def _check_notice(self, args: dict[str, Any]) -> dict[str, Any] | None:
        if (b := self._need_identity("issue_graduation_notice")) is not None:
            return b
        p = self.identity
        assert p is not None
        if p.graduate is None:
            return self._block("G5", "not_a_graduate", "issue_graduation_notice", "The person is not a graduate.")
        if norm_digits(args["national_number"]).strip() != p.national_number:
            return self._mismatch("issue_graduation_notice", "national_number")
        if norm_text(args["full_name"]) != norm_text(p.graduate.get("full_name") or p.full_name):
            return self._mismatch("issue_graduation_notice", "full_name")
        if norm_text(args["major"]) != norm_text(p.major):
            return self._mismatch("issue_graduation_notice", "major")
        if norm_date(args["graduation_date"]) != norm_date(p.graduation_date):
            return self._mismatch("issue_graduation_notice", "graduation_date")
        return None

    def _check_email(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        """G1: only the registered address of the identified person, only this run's attachments."""
        if (b := self._need_identity("send_email")) is not None:
            return b
        p = self.identity
        assert p is not None
        to = str(args["to"])
        if re.search(r"[,;\s]", to.strip()) or to.strip().startswith("["):
            return self._block("G1", "multiple_recipients", "send_email", "One recipient only.")
        if p.registered_email is None or norm_email(to) != p.registered_email:
            return self._block("G1", "recipient_not_registered", "send_email",
                               "Email may only be sent to the applicant's registered address.")
        att = args.get("attachment_id")
        if att:
            own = {r.get("id") for r in ctx.state.side_effects(ctx.run_id).get("graduation_notices", [])}
            own |= {f"pending:{q['id']}" for q in self.pending if q["tool"] == "issue_graduation_notice"}
            if att not in own:
                return self._block("G1", "unknown_attachment", "send_email",
                                   "attachment_id must come from a notice issued in this request.")
        for f in ("subject", "body"):
            if len(args[f]) > LIMITS[f]:
                return self._block("G1", "argument_too_long", "send_email", f"{f} is too long.", field=f)
        foreign = {norm_email(a) for a in _EMAIL.findall(args["body"] + " " + args["subject"])} - {p.registered_email}
        if foreign:
            return self._block("G1", "foreign_address_in_body", "send_email",
                               "The email must not contain other email addresses.")
        others = set(national_numbers(args["body"])) - {p.national_number}
        if others:
            return self._block("G1", "other_person_data_in_body", "send_email",
                               "The email must not contain another person's national number.")
        return None

    def _budget(self, ctx: ToolContext, table: str, tool: str) -> dict[str, Any] | None:
        """G7: at most one email, one registration and one notice per run (performed or queued)."""
        done = len(ctx.state.side_effects(ctx.run_id).get(table, [])) + sum(1 for q in self.pending if q["tool"] == tool)
        if done >= 1:
            reason = "email_budget_exceeded" if table == "outbox" else "action_budget_exceeded"
            return self._block("G7", reason, tool, "This request already performed this action once.")
        return None

    def _key(self, tool: str) -> str | None:
        """G15 key of an action: service + person (+ day for transcripts). The scope separates test runs."""
        p = self.identity
        if p is None:
            return None
        if tool == "register_masters":
            return f"{self.scope}masters_registration:{p.national_number}"
        if tool == "issue_graduation_notice":
            return f"{self.scope}graduation_notice:{p.national_number}"
        if tool == "send_email" and self.service == "transcript":
            return f"{self.scope}transcript_email:{p.national_number}"
        return None  # other e-mails belong to an action that already has a key

    def _idempotent(self, tool: str, ctx: ToolContext) -> dict[str, Any] | None:
        """G15: the same action for the same person is performed once (claimed before it runs)."""
        key = self._key(tool)
        if key is None or self.state is None:
            return None
        earlier = self.state.claim_idempotency_key(key, ctx.run_id, max_age_s=self._max_age_s(tool))
        if earlier is not None and earlier.get("run_id") != ctx.run_id:
            self.duplicate_blocks += 1
            return self._block("G15", "duplicate_request", tool,
                               "This was already done for this person in an earlier request. Do not repeat it; "
                               "finish and say so.", first_done_at=earlier.get("created_at"))
        return None

    def _maybe_queue(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
        """G16: in human-approval mode the action is stored for a staff member instead of being performed."""
        if tool not in ACTION_TOOLS or self.human_approval == "off":
            return None
        if self.human_approval == "flagged" and self.verdict == "approved" and not self.flags:
            return None
        why = "every_action" if self.human_approval == "actions" else ("needs_review" if self.verdict != "approved"
                                                                       else "flagged:" + ",".join(self.flags))
        pid = ctx.state.add_pending_action(ctx.run_id, len(self.pending) + 1, tool, args, why)
        self.pending.append({"id": pid, "tool": tool})
        self._event("G16", "queued", "waiting_for_staff_approval", tool, pending_action_id=pid, why=why)
        out: dict[str, Any] = {"status": "queued_for_staff_approval", "pending_action_id": pid,
                               "note": "A staff member must approve this before it happens. Do not repeat it."}
        if tool == "issue_graduation_notice":
            out["attachment_id"] = f"pending:{pid}"
        return out

    # -- after the loop ------------------------------------------------------

    def finalize(self, decision: str | None, final_output: Any, ctx: ToolContext) -> str | None:
        t0 = time.perf_counter()
        self.model_decision = decision
        if not self._valid_final(final_output):
            self._event("G6", "flagged", "invalid_final_json")
        if self._staff_decides():
            # the rules only recommend; the request waits for a staff member's yes or no
            self.awaiting_staff = True
            self._event("G16", "flagged", "final_decision_by_staff", recommendation=self.verdict,
                        model_decision=decision, reasons=list(self.reasons) + list(self.flags))
            self.latency_ms += (time.perf_counter() - t0) * 1000.0
            return "needs_review"
        if decision != self.verdict:
            self._event("G6", "overridden", "decision_from_policy", model_decision=decision, verdict=self.verdict)
        self.latency_ms += (time.perf_counter() - t0) * 1000.0
        return self.verdict

    @staticmethod
    def _valid_final(out: Any) -> bool:
        return (
            isinstance(out, dict)
            and out.get("decision") in DECISIONS
            and isinstance(out.get("reason"), str)
            and isinstance(out.get("extracted_fields"), dict)
            and isinstance(out.get("actions_taken"), list)
            and all(isinstance(a, str) for a in out["actions_taken"])
        )
