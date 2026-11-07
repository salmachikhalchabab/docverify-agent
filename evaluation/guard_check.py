"""What the guardrails decide BEFORE the model runs, for every case. No model, no API key.

    python evaluation/guard_check.py            # all cases
    python evaluation/guard_check.py A1 V4      # some cases

For each case: the policy verdict, its reasons, and whether it matches the case's expected
decisions. Use it after changing a rule: a valid case that is not "approved" here would be
a false block in the real evaluation.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.datastore import SQLiteStateStore, get_reference_store  # noqa: E402
from agent.documents import load_document  # noqa: E402
from agent.guards import DeterministicGuardrails  # noqa: E402
from config import get_settings  # noqa: E402
from evaluation.run_eval import CASES_PATH, seed_prior_actions, use_cases_registry  # noqa: E402
from evaluation.scoring import load_cases  # noqa: E402


def main() -> int:
    use_cases_registry()
    s = get_settings()
    ref = get_reference_store(s.reference_backend, s.reference_db_path)
    state = SQLiteStateStore(Path(tempfile.mkdtemp()) / "state.sqlite")
    cases = load_cases(CASES_PATH)
    if len(sys.argv) > 1:
        cases = [c for c in cases if any(c.id.startswith(a) for a in sys.argv[1:])]
    bad = 0
    for c in cases:
        docs = [load_document(ROOT / d, dpi=s.page_render_dpi, max_image_side=s.max_image_side,
                              max_pages=s.max_pages_per_document, ocr_engine=s.ocr_engine, ocr_lang=s.ocr_lang)
                for d in c.documents]
        scope = f"guard_check/{c.id}/"
        seed_prior_actions(c, scope, state)
        g = DeterministicGuardrails(service=c.service, requester_email=c.requester_email, today=c.today,
                                    reference=ref, state=state, ocr_engine=s.ocr_engine, ocr_lang=s.ocr_lang,
                                    hidden_text_threshold=s.hidden_text_threshold, idempotency_scope=scope)
        g.transform_documents(docs)
        ok = g.verdict in c.expected_decisions
        bad += not ok
        kinds = ",".join(f"{d.kind}{'' if d.name_confirmed else '(name?)'}" for d in g.docs)
        print(f"{'ok ' if ok else 'BAD'} {c.id:<34} verdict={g.verdict:<12} expected={'/'.join(c.expected_decisions):<22} "
              f"{g.latency_ms / 1000:5.1f}s  docs=[{kinds}]")
        if g.reasons or g.flags:
            print(f"     reasons={g.reasons} flags={g.flags}")
    print(f"\n{len(cases) - bad}/{len(cases)} cases get an expected verdict before the model runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
