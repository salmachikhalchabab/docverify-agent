"""What the guardrails decide BEFORE the model runs, on the P1 dev cases. No model, no API key, no cost.

    python evaluation/p1_guard_check.py                    # every dev case
    python evaluation/p1_guard_check.py --labels valid     # only the valid ones (false blocks)
    python evaluation/p1_guard_check.py --cases <id> -v    # one case, with the text the rules read

For each case: the rules' verdict and reasons, whether the verdict is one P1 accepts, and per document
the kind the rules recognised (national_id, transcript, ...), the national numbers they found and
whether the person's name was confirmed. With -v also the start of the OCR text the rules read.

Dev split only: the test split is kept for the final run and is not looked at while the rules change.
Everything printed stays on this computer (fictional P1 data).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent.datastore import SQLiteStateStore, get_reference_store  # noqa: E402
from agent.documents import load_document  # noqa: E402
from agent.guards import DeterministicGuardrails, norm_text  # noqa: E402
from config import get_settings  # noqa: E402
from evaluation.p1_eval import (  # noqa: E402
    DECISION_TO_P1, UnavailableRegistry, labels_path, load_cases, p1_root, registered_email, select,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", help="comma-separated opaque_case_ids")
    ap.add_argument("--labels", help="only these case types, e.g. valid")
    ap.add_argument("-v", "--verbose", action="store_true", help="also print the start of each document's text")
    a = ap.parse_args()

    s = get_settings()
    if s.reference_backend != "p1_sqlite":
        raise SystemExit(f"REFERENCE_BACKEND must be p1_sqlite (it is {s.reference_backend!r})")
    reference = get_reference_store(s.reference_backend, s.reference_db_path)
    state = SQLiteStateStore(Path(tempfile.mkdtemp()) / "state.sqlite")
    cases, meta = load_cases(labels_path())
    chosen = select(cases, "dev", a.cases, a.labels)
    root = p1_root()
    today = str(meta.get("as_of_date"))
    bad = 0
    for c in chosen:
        docs = [load_document(root / d, dpi=s.page_render_dpi, max_image_side=s.max_image_side,
                              max_pages=s.max_pages_per_document, ocr_engine=s.ocr_engine, ocr_lang=s.ocr_lang)
                for d in c.documents]
        ref = UnavailableRegistry(reference, c.registry_failure) if c.registry_failure else reference
        g = DeterministicGuardrails(service=c.service, requester_email=registered_email(reference, c.requester_nid),
                                    today=today, reference=ref, state=state, ocr_engine=s.ocr_engine,
                                    ocr_lang=s.ocr_lang, hidden_text_threshold=s.hidden_text_threshold,
                                    idempotency_scope=f"p1_guard_check/{c.case_id}/")
        g.transform_documents(docs)
        ok = DECISION_TO_P1.get(g.verdict) in c.acceptable
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {c.case_id[:12]} {c.label:<10} {c.service:<20} verdict={g.verdict:<12} "
              f"P1 accepts={'/'.join(c.acceptable)}")
        if g.reasons or g.flags:
            print(f"     reasons={g.reasons} flags={g.flags}")
        for d, path in zip(g.docs, c.documents):
            nn = ",".join(sorted(d.national_numbers)) or "none"
            print(f"     doc {d.index} {Path(path).name}: kind={d.kind} national_numbers={nn} "
                  f"name_confirmed={d.name_confirmed} qr={len(d.qr_codes)}")
            if a.verbose:
                print(f"       text: {norm_text(d.text)[:300]!r}")
    print(f"\n{len(chosen) - bad}/{len(chosen)} dev cases get a verdict P1 accepts before the model runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
