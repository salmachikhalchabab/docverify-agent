"""Run evaluation cases through run_agent() and score them.

    python evaluation/run_eval.py --dry-run                  # validate cases, show plan; no model calls
    python evaluation/run_eval.py --cases A1_hidden_exfil_transcript,D1_masters_expired
    python evaluation/run_eval.py --cases attack --repeats 3
    python evaluation/run_eval.py --cases all --variant baseline

Each run's full trace goes to runs/traces.jsonl as usual. One scored line per
run is appended to runs/eval_results.jsonl (linked by run_id).
Stops at the first rate-limit error unless --continue-on-error.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):  # Arabic output on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent.run import run_agent  # noqa: E402
from config import Settings, get_settings  # noqa: E402
from evaluation.report import summarize  # noqa: E402
from evaluation.scoring import Case, load_cases, score  # noqa: E402

CASES_PATH = ROOT / "evaluation" / "cases.yaml"
EST_REQUESTS_PER_RUN = 6  # rough, for the dry-run budget estimate


def select(cases: list[Case], spec: str) -> list[Case]:
    if spec == "all":
        return cases
    wanted = {s.strip() for s in spec.split(",") if s.strip()}
    chosen = [c for c in cases if c.id in wanted or c.category in wanted]
    unknown = wanted - {c.id for c in chosen} - {c.category for c in chosen}
    if unknown:
        raise SystemExit(f"unknown case ids/categories: {sorted(unknown)}")
    return chosen


def run_case(case: Case, repeat: int, batch: str, settings: Settings, variant: str | None = None, **inject: Any) -> dict[str, Any]:
    r = run_agent(
        service=case.service,
        requester_email=case.requester_email,
        documents=[str(ROOT / d) for d in case.documents],
        variant=variant,
        settings=settings,
        today=date.fromisoformat(case.today),
        **inject,
    )
    t = r.trace
    m = r.metrics
    return {
        "eval_batch": batch,
        "timestamp": t.timestamp,
        "case_id": case.id,
        "category": case.category,
        "variant": t.variant,
        "model": t.model,
        "repeat": repeat,
        "run_id": r.run_id,
        "decision": r.decision,
        "status": t.status,
        "parse_error": t.parse_error,
        "hit_max_steps": t.hit_max_steps,
        "error": t.error.splitlines()[0][:300] if t.error else None,
        **score(case, r.decision, r.side_effects.get("rows", {})),
        "metrics": {k: m.get(k) for k in (
            "total_cost_usd", "cost_complete", "total_latency_ms", "total_tokens", "input_tokens",
            "output_tokens", "reasoning_tokens", "num_model_calls", "num_tool_calls")},
    }


def append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default="all", help="'all', case ids and/or categories, comma-separated")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--variant", default=None)
    ap.add_argument("--max-steps", type=int, default=None, help="override MAX_STEPS for this batch")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--continue-on-error", action="store_true")
    a = ap.parse_args()

    cases = select(load_cases(CASES_PATH), a.cases)
    settings = get_settings()
    if a.max_steps:
        settings = replace(settings, max_steps=a.max_steps)
    n_runs = len(cases) * a.repeats

    print(f"{len(cases)} cases x {a.repeats} repeats = {n_runs} runs | model {settings.llm_model} | "
          f"variant {a.variant or settings.agent_variant} | max_steps {settings.max_steps} | "
          f"~{n_runs * EST_REQUESTS_PER_RUN} model requests (rough)")
    if a.dry_run:
        for c in cases:
            atk = f" | attack: {c.attack['channel']} -> {c.attack['goal']}" if c.attack else ""
            print(f"  {c.id:<36} {c.category:<9} {c.service:<20} today={c.today} docs={len(c.documents)} "
                  f"expect={'/'.join(c.expected_decisions)}{atk}")
        return 0

    batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = settings.runs_dir / "eval_results.jsonl"
    rows = []
    for c in cases:
        for rep in range(1, a.repeats + 1):
            row = run_case(c, rep, batch, settings, a.variant)
            append(out, row)
            rows.append(row)
            flag = "ERROR " + (row["error"] or "") if row["status"] == "error" else "TRUNCATED (max_tokens)" if row["status"] == "truncated" else (
                f"decision={row['decision']} ok={row['decision_correct']} safe={row['safe']}"
                + (f" violations={row['violations']}" if row["violations"] else "")
                + (f" ATTACK={'SUCCESS' if row['attack_success'] else 'failed'}" if row["attack_success"] is not None else ""))
            print(f"[{c.id} #{rep}] {flag}  ({row['metrics']['num_model_calls']} calls, "
                  f"{(row['metrics']['total_latency_ms'] or 0) / 1000:.1f}s)", flush=True)
            if row["status"] == "error" and "RateLimit" in (row["error"] or "") and not a.continue_on_error:
                print("stopping: rate limited (use --continue-on-error to keep going)")
                summarize(rows)
                return 1
    print(f"\nresults appended to {out} (batch {batch})\n")
    summarize(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
