"""Summarize runs/eval_results.jsonl.

    python evaluation/report.py                 # all results
    python evaluation/report.py --batch <id>    # one batch
    python evaluation/report.py --variant baseline

Runs that ended in an API/runtime error, or whose final reply was cut off by
the max_tokens setting, are counted ("err") but excluded from the rates.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if hasattr(sys.stdout, "reconfigure"):  # Arabic output on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# Runs that say nothing about the agent's behavior: API/runtime errors, and replies
# cut off by the max_tokens setting. Counted in the "err" column, excluded from rates.
EXCLUDED = {"error", "truncated"}


def _rate(rows: list[dict[str, Any]], key: str) -> str:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return f"{mean(vals):.0%}" if vals else "-"


def _avg(rows: list[dict[str, Any]], key: str, scale: float = 1.0, fmt: str = "{:.1f}") -> str:
    vals = [r["metrics"][key] for r in rows if r["metrics"].get(key) is not None]
    return fmt.format(mean(vals) / scale) if vals else "-"


def summarize(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("no results")
        return
    by_group: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_group[(r["variant"], r["model"], (r.get("prompt_sha256") or "-")[:8], r["category"])].append(r)

    header = (f"{'variant':<9} {'model':<22} {'prompt':<8} {'category':<10} {'runs':>4} {'err':>3} {'decision':>8} {'task':>5} "
              f"{'unsafe':>6} {'ASR':>4} {'acted!':>6} {'parse':>5} {'maxst':>5} {'$/run':>8} {'s/run':>6} {'tok/run':>8}")
    print(header)
    print("-" * len(header))
    for (variant, model, prompt, cat), rs in sorted(by_group.items()):
        ok = [r for r in rs if r["status"] not in EXCLUDED]
        unsafe = [dict(r, unsafe=not r["safe"]) for r in ok]
        print(f"{variant:<9} {model.split('/')[-1][:22]:<22} {prompt:<8} {cat:<10} {len(rs):>4} {len(rs) - len(ok):>3} "
              f"{_rate(ok, 'decision_correct'):>8} {_rate(ok, 'task_success'):>5} {_rate(unsafe, 'unsafe'):>6} "
              f"{_rate(ok, 'attack_success'):>4} {_rate(ok, 'acted_without_approval'):>6} "
              f"{_rate(ok, 'parse_error'):>5} {_rate(ok, 'hit_max_steps'):>5} "
              f"{_avg(ok, 'total_cost_usd', fmt='{:.4f}'):>8} {_avg(ok, 'total_latency_ms', 1000):>6} "
              f"{_avg(ok, 'total_tokens', fmt='{:.0f}'):>8}")
    print("\ndecision=decision correct, task=correct decision + required effects + no violations,"
          "\nunsafe=any forbidden effect happened, ASR=attack success rate, acted!=service action without approving,"
          "\nprompt=first 8 hex digits of the system prompt's sha256 (results of different prompts are never pooled)")

    print("\nper case:")
    by_case: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_case[(r["variant"], r["model"], r["case_id"])].append(r)
    for (variant, model, cid), rs in sorted(by_case.items()):
        ok = [r for r in rs if r["status"] not in EXCLUDED]
        dec = defaultdict(int)
        for r in ok:
            dec[str(r["decision"])] += 1
        viol = defaultdict(int)
        for r in ok:
            for v in r["violations"]:
                viol[v] += 1
        atk = [r["attack_success"] for r in ok if r["attack_success"] is not None]
        print(f"  {variant:<9} {model.split('/')[-1][:22]:<22} {cid:<34} n={len(ok)}{f' (+{len(rs) - len(ok)} err)' if len(rs) > len(ok) else ''} "
              f"decisions={dict(dec)}" + (f" violations={dict(viol)}" if viol else "")
              + (f" attack_success={sum(atk)}/{len(atk)}" if atk else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "runs" / "eval_results.jsonl"))
    ap.add_argument("--batch")
    ap.add_argument("--variant")
    a = ap.parse_args()
    p = Path(a.file)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []
    rows = [r for r in rows if (not a.batch or r["eval_batch"] == a.batch) and (not a.variant or r["variant"] == a.variant)]
    summarize(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
