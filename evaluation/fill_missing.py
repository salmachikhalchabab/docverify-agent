"""Re-run only the runs an experiment is missing (API errors, daily quota), then rebuild the report.

    python evaluation/fill_missing.py --since 20261107T122540Z --model gemini/gemini-3.5-flash-lite \
        --plan baseline:3,guarded:3,prompt_only:1,tools_only:1 --markdown docs/results_2026-11-07.md

For every (variant, case) it counts the successful runs since --since with --model, and runs the
difference to reach the planned number of repeats. Results are appended to runs/eval_results.jsonl
in a new batch, so the report (with the same --since) includes them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from evaluation.report import EXCLUDED, markdown, summarize  # noqa: E402
from evaluation.run_eval import CASES_PATH, append, run_case, use_cases_registry  # noqa: E402
from evaluation.scoring import load_cases  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--plan", required=True, help="variant:repeats,variant:repeats,...")
    ap.add_argument("--markdown")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    os.environ["LLM_MODEL"] = a.model  # before settings are read
    use_cases_registry()

    from config import get_settings

    settings = replace(get_settings(), human_approval="off")
    out = settings.runs_dir / "eval_results.jsonl"

    def rows() -> list[dict]:
        if not out.exists():
            return []
        rs = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [r for r in rs if r["eval_batch"] >= a.since and r["model"] == a.model]

    good = Counter((r["variant"], r["case_id"]) for r in rows() if r["status"] not in EXCLUDED)
    plan = {p.split(":")[0]: int(p.split(":")[1]) for p in a.plan.split(",")}
    cases = load_cases(CASES_PATH)
    todo = [(v, c, rep) for v, n in plan.items() for c in cases for rep in range(good[(v, c.id)] + 1, n + 1)]
    print(f"{len(todo)} runs missing")
    for v, c, rep in todo:
        print(f"  {v:<12} {c.id}")
    if a.dry_run or not todo:
        return 0
    batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for i, (v, c, rep) in enumerate(todo, 1):
        row = run_case(c, rep, batch, settings, v)
        append(out, row)
        print(f"[{i}/{len(todo)}] {v} {c.id} #{rep}: {row['status']} decision={row['decision']} "
              f"{(row['error'] or '')[:80]}", flush=True)
        if row["status"] == "error" and "RateLimit" in (row["error"] or "") and "per day" in (row["error"] or ""):
            print("daily quota reached: stopping. Run this again after the quota resets.")
            break
    final = rows()
    summarize(final)
    if a.markdown:
        Path(a.markdown).write_text(markdown(final), encoding="utf-8")
        print(f"\nwrote {a.markdown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
