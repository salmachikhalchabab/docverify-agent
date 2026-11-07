"""Send already-recorded runs (runs/traces.jsonl) to Langfuse, with their evaluation scores.

    python evaluation/langfuse_backfill.py --dry-run     # how many runs would be sent
    python evaluation/langfuse_backfill.py               # send the ones not sent yet
    python evaluation/langfuse_backfill.py --since 2026-11-07

Needs LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY (and LANGFUSE_BASE_URL for the US region) in .env.
Every record is redacted before it leaves the machine (old traces were saved with document text).
Run ids already sent are kept in runs/.langfuse_exported, so running it twice sends nothing twice.
Scores come from runs/eval_results.jsonl and runs/p1_results.jsonl when the run appears there.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent import observability  # noqa: E402
from agent.datastore import get_reference_store  # noqa: E402
from agent.privacy import redact_trace, secret_key  # noqa: E402
from agent.trace import read_traces  # noqa: E402
from config import get_settings  # noqa: E402

SCORE_KEYS = ("decision_correct", "task_success", "safe", "attack_success", "acted_without_approval")


def load_scores(paths: list[Path]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            s = {k: row.get(k) for k in SCORE_KEYS}
            if isinstance(row.get("trajectory"), dict):
                s["trajectory_recall"] = row["trajectory"].get("recall")
            s["case"] = row.get("case_id")
            out[row["run_id"]] = s
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="only runs whose timestamp is >= this (e.g. 2026-11-07)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    s = get_settings()
    done_path = s.runs_dir / ".langfuse_exported"
    done = set(done_path.read_text(encoding="utf-8").split()) if done_path.exists() else set()
    traces = [t for t in read_traces(s.traces_path)
              if t.get("run_id") not in done and (not a.since or str(t.get("timestamp", "")) >= a.since)]
    scores = load_scores([s.runs_dir / "eval_results.jsonl", s.runs_dir / "p1_results.jsonl"])
    print(f"{len(traces)} run(s) to send ({len(done)} already sent), {sum(t['run_id'] in scores for t in traces)} with scores")
    if a.dry_run or not traces:
        return 0
    if not observability.enabled():
        raise SystemExit("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env first.")

    try:
        reference = get_reference_store(s.reference_backend, s.reference_db_path)
    except Exception:  # noqa: BLE001 - redaction still removes document text, numbers and e-mails
        reference = None
    key = secret_key(s.runs_dir)
    sent = 0
    with done_path.open("a", encoding="utf-8") as done_file:
        for t in traces:
            record = t if t.get("pii") == "redacted" else redact_trace(t, reference, key)
            try:
                observability.export_trace(record)
                if t["run_id"] in scores:
                    observability.score_run(t["run_id"], scores[t["run_id"]])
            except Exception as e:  # noqa: BLE001
                print(f"  failed {t['run_id'][:12]}: {e}")
                continue
            done_file.write(t["run_id"] + "\n")
            sent += 1
    observability.flush()
    print(f"sent {sent} run(s) to Langfuse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
