"""Personal data minimisation for traces (TRACE_PII=redact, the default).

What a saved trace keeps and loses:
  - document text (every page, as extracted and as sent to the model): NOT stored, only its length
  - national numbers, student numbers, names (full, first, family, father, mother, English),
    birth dates and places of every person whose national number appears in the run,
    e-mail addresses (local part) and phone numbers: replaced by stable pseudonyms
        14749200009                -> <NN:3f9a1c0b>
        نور السيد                   -> <NAME:a71e02d4>
        nour.alsayed@example.com   -> user-5c0e9b21@example.com   (domain kept: it shows WHERE mail went)
  - everything else (steps, tool names, decisions, metrics, guard events) is unchanged.

A pseudonym is HMAC-SHA256(secret, value), first 8 hex digits: the same value always gets the same
pseudonym (runs stay comparable), but it cannot be turned back without the secret. The secret
comes from TRACE_PSEUDONYM_KEY or, if unset, from runs/.pseudonym_key (created once, git-ignored).

The operational records the service needs (the state database: outbox, registrations, notices)
are not changed here; they are the system's data, not research logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from agent.datastore import ReferenceStore, UnknownRegistryError

_DIGITS = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")} | {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_NATIONAL = re.compile(r"(?<!\d)\d{11}(?!\d)")
_EMAIL = re.compile(r"([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)")
_PHONE = re.compile(r"(?<!\d)(?:\+?963|00963|0)?9\d{8}(?!\d)")
NAME_FIELDS = ("full_name", "first_name", "family_name", "father_name", "mother_name", "full_name_en", "holder_name")
OTHER_FIELDS = {"birth_date": "DATE", "birth_place": "PLACE", "student_id": "SID"}


def secret_key(runs_dir: Path) -> bytes:
    env = os.getenv("TRACE_PSEUDONYM_KEY", "").strip()
    if env:
        return env.encode()
    path = runs_dir / ".pseudonym_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip().encode()


def pseudonym(value: str, key: bytes) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:8]


def _lookup(reference: ReferenceStore | None, registry: str, key: str) -> dict[str, Any] | None:
    if reference is None or registry not in reference.registries():
        return None
    try:
        return reference.lookup(registry, key)
    except UnknownRegistryError:
        return None


def known_values(blob: str, reference: ReferenceStore | None, key: bytes) -> dict[str, str]:
    """value -> replacement, for every person whose national number appears in `blob`."""
    out: dict[str, str] = {}
    for nn in set(_NATIONAL.findall(blob.translate(_DIGITS))):
        out[nn] = f"<NN:{pseudonym(nn, key)}>"
        out[nn.translate({ord(str(i)): c for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})] = out[nn]
        records = [r for r in (_lookup(reference, reg, nn) for reg in ("civil_registry", "students")) if r]
        for rec in records:
            for f in NAME_FIELDS:
                v = str(rec.get(f) or "").strip()
                if not v:
                    continue
                out[v] = f"<NAME:{pseudonym(v, key)}>"
                for part in v.split():  # single name words too ("نور", "السيد"), 3+ letters only
                    if len(part) >= 3:
                        out.setdefault(part, f"<NAME:{pseudonym(part, key)}>")
            for f, label in OTHER_FIELDS.items():
                v = str(rec.get(f) or "").strip()
                if v:
                    out[v] = f"<{label}:{pseudonym(v, key)}>"
    return out


class Redactor:
    def __init__(self, values: dict[str, str], key: bytes):
        self.key = key
        # longest first, so "نور السيد" is replaced before "نور"
        parts = sorted(values, key=len, reverse=True)
        self.values = values
        self.pattern = re.compile("|".join(rf"(?<![\w]){re.escape(p)}(?![\w])" for p in parts)) if parts else None

    def text(self, s: str) -> str:
        if self.pattern is not None:
            s = self.pattern.sub(lambda m: self.values[m.group(0)], s)
        s = _EMAIL.sub(lambda m: f"user-{pseudonym(m.group(1).lower(), self.key)}@{m.group(2)}", s)
        s = _PHONE.sub(lambda m: f"<PHONE:{pseudonym(m.group(0), self.key)}>", s)
        return s

    def value(self, v: Any) -> Any:
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, dict):
            return {k: self.value(x) for k, x in v.items()}
        return v


def _drop_document_text(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in docs or []:
        d = dict(d)
        d["pages"] = [{**p, "text": f"[not stored: {len(p.get('text') or '')} characters]"} for p in d.get("pages", [])]
        out.append(d)
    return out


def redact_trace(trace: dict[str, Any], reference: ReferenceStore | None, key: bytes) -> dict[str, Any]:
    """A copy of the trace dict that is safe to keep (see module docstring)."""
    blob = json.dumps(trace, ensure_ascii=False, default=str)
    red = Redactor(known_values(blob, reference, key), key)
    out = dict(trace)
    out["documents"] = _drop_document_text(trace.get("documents", []))
    out["documents_as_sent"] = _drop_document_text(trace.get("documents_as_sent", []))
    for k, v in out.items():
        if k not in {"documents", "documents_as_sent", "metrics", "config"}:
            out[k] = red.value(v)
    out["documents"] = [{**d, "path": red.text(str(d.get("path", "")))} for d in out["documents"]]
    out["pii"] = "redacted"
    return out
