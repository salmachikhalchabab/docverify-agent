"""Summarize runs/eval_results.jsonl (or runs/p1_results.jsonl with --file).

    python evaluation/report.py                 # all results
    python evaluation/report.py --batch <id>    # one batch
    python evaluation/report.py --variant baseline
    python evaluation/report.py --since 20261107T1219 --markdown docs/results.md   # this experiment, as a file

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

    header = (f"{'variant':<17} {'model':<22} {'prompt':<8} {'category':<10} {'runs':>4} {'err':>3} {'decision':>8} {'task':>5} "
              f"{'unsafe':>6} {'ASR':>4} {'acted!':>6} {'parse':>5} {'maxst':>5} {'blocks':>6} {'removed':>7} "
              f"{'fblock':>6} {'agent':>5} {'ovr':>4} {'staff':>5} {'$/run':>8} {'s/run':>6} {'tok/run':>8}")
    print(header)
    print("-" * len(header))
    for (variant, model, prompt, cat), rs in sorted(by_group.items()):
        ok = [r for r in rs if r["status"] not in EXCLUDED]
        unsafe = [dict(r, unsafe=not r["safe"]) for r in ok]
        print(f"{variant:<17} {model.split('/')[-1][:22]:<22} {prompt:<8} {cat:<10} {len(rs):>4} {len(rs) - len(ok):>3} "
              f"{_rate(ok, 'decision_correct'):>8} {_rate(ok, 'task_success'):>5} {_rate(unsafe, 'unsafe'):>6} "
              f"{_rate(ok, 'attack_success'):>4} {_rate(ok, 'acted_without_approval'):>6} "
              f"{_rate(ok, 'parse_error'):>5} {_rate(ok, 'hit_max_steps'):>5} "
              f"{_avg(ok, 'guard_blocks'):>6} {_avg(ok, 'hidden_text_chars_removed', fmt='{:.0f}'):>7} "
              f"{_rate(ok, 'false_block'):>6} "
              f"{_rate(ok, 'model_decision_correct'):>5} {_rate(ok, 'overridden_by_rules'):>4} {_rate(ok, 'sent_to_staff'):>5} "
              f"{_avg(ok, 'total_cost_usd', fmt='{:.4f}'):>8} {_avg(ok, 'total_latency_ms', 1000):>6} "
              f"{_avg(ok, 'total_tokens', fmt='{:.0f}'):>8}")
    print("\ndecision=decision correct, task=correct decision + required effects + no violations,"
          "\nunsafe=any forbidden effect happened, ASR=attack success rate, acted!=service action without approving,"
          "\nprompt=first 8 hex digits of the system prompt's sha256 (results of different prompts are never pooled)"
          "\nblocks=guardrail refusals per run, removed=hidden text chars removed per run, fblock=valid case with a refusal"
          "\nagent=the agent's own decision correct (before rules or staff), ovr=final decision changed by the rules,"
          "\nstaff=sent to a (simulated) staff member")

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
        print(f"  {variant:<17} {model.split('/')[-1][:22]:<22} {cid:<34} n={len(ok)}{f' (+{len(rs) - len(ok)} err)' if len(rs) > len(ok) else ''} "
              f"decisions={dict(dec)}" + (f" violations={dict(viol)}" if viol else "")
              + (f" attack_success={sum(atk)}/{len(atk)}" if atk else ""))


VARIANT_ORDER = ["baseline", "prompt_only", "tools_only", "guarded", "baseline_human", "prompt_only_human",
                 "guarded_human"]


def _pct(rows: list[dict[str, Any]], key: str, invert: bool = False) -> str:
    vals = [bool(r[key]) != invert for r in rows if r.get(key) is not None]
    return f"{mean(vals):.0%} ({sum(vals)}/{len(vals)})" if vals else "-"


def markdown(rows: list[dict[str, Any]]) -> str:
    """The comparison as Markdown: one table per question (security, success, cost, latency) and per case."""
    ok = [r for r in rows if r["status"] not in EXCLUDED]
    variants = sorted({r["variant"] for r in rows}, key=lambda v: VARIANT_ORDER.index(v) if v in VARIANT_ORDER else 99)
    by = {v: [r for r in ok if r["variant"] == v] for v in variants}
    models = sorted({r["model"] for r in rows})
    out = [f"Model: {', '.join(models)}. Runs: {len(rows)} ({len(rows) - len(ok)} excluded: API errors or cut-off replies). "
           f"Batches: {', '.join(sorted({r['eval_batch'] for r in rows}))}.", ""]

    def table(title: str, cols: list[tuple[str, Any]]) -> None:
        out.extend([f"### {title}", "", "| variant | " + " | ".join(c for c, _ in cols) + " |",
                    "|---|" + "---|" * len(cols)])
        for v in variants:
            out.append(f"| {v} | " + " | ".join(f(by[v]) for _, f in cols) + " |")
        out.append("")

    att = lambda rs: [r for r in rs if r.get("attack_success") is not None]  # noqa: E731  attack cases
    val = lambda rs: [r for r in rs if r["category"] == "valid"]  # noqa: E731
    table("Security (lower is better)", [
        ("attack success rate", lambda rs: _pct(att(rs), "attack_success")),
        ("unsafe runs, all cases", lambda rs: _pct(rs, "safe", invert=True)),
        ("unsafe runs, attacks", lambda rs: _pct(att(rs), "safe", invert=True)),
        ("acted without approving", lambda rs: _pct(rs, "acted_without_approval")),
    ])
    table("Success (higher is better)", [
        ("task success, all", lambda rs: _pct(rs, "task_success")),
        ("task success, valid cases", lambda rs: _pct(val(rs), "task_success")),
        ("correct decision, all", lambda rs: _pct(rs, "decision_correct")),
        ("false blocks on valid cases", lambda rs: _pct(val(rs), "false_block")),
    ])
    staffed = lambda rs: [dict(r, staff_approved=r["staff_review"]["approved"]) for r in rs  # noqa: E731
                          if r.get("staff_review")]
    table("Who decided (autonomy and human involvement)", [
        ("agent's own decision correct", lambda rs: _pct(rs, "model_decision_correct")),
        ("final decision changed by the rules", lambda rs: _pct(rs, "overridden_by_rules")),
        ("sent to staff", lambda rs: _pct(rs, "sent_to_staff")),
        ("staff approved (of those sent)", lambda rs: _pct(staffed(rs), "staff_approved")),
    ])
    table("Cost and latency per run (mean)", [
        ("tokens", lambda rs: _avg(rs, "total_tokens", fmt="{:,.0f}")),
        ("model calls", lambda rs: _avg(rs, "num_model_calls")),
        ("cost USD (list price)", lambda rs: _avg(rs, "total_cost_usd", fmt="{:.4f}")),
        ("seconds", lambda rs: _avg(rs, "total_latency_ms", 1000)),
        ("guardrail seconds", lambda rs: _avg(rs, "guard_latency_ms", 1000, fmt="{:.2f}")),
        ("refusals", lambda rs: _avg(rs, "guard_blocks")),
    ])
    out.extend(["### Per case: correct decision / attack success", "",
                "| case | " + " | ".join(variants) + " |", "|---|" + "---|" * len(variants)])
    for cid in sorted({r["case_id"] for r in rows}):
        cells = []
        for v in variants:
            rs = [r for r in by[v] if r["case_id"] == cid]
            if not rs:
                cells.append("-")
                continue
            c = f"{sum(r['decision_correct'] for r in rs)}/{len(rs)} ok"
            if any(r["attack_success"] is not None for r in rs):
                c += f", attack {sum(bool(r['attack_success']) for r in rs)}/{len(rs)}"
            if any(not r["safe"] for r in rs):
                c += f", unsafe {sum(not r['safe'] for r in rs)}"
            cells.append(c)
        out.append(f"| {cid} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "runs" / "eval_results.jsonl"))
    ap.add_argument("--batch")
    ap.add_argument("--since", help="only batches whose id is >= this (ids are UTC timestamps)")
    ap.add_argument("--variant")
    ap.add_argument("--model")
    ap.add_argument("--markdown", help="also write the comparison as a Markdown file")
    a = ap.parse_args()
    p = Path(a.file)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []
    rows = [r for r in rows if (not a.batch or r["eval_batch"] == a.batch) and (not a.variant or r["variant"] == a.variant)
            and (not a.since or r["eval_batch"] >= a.since) and (not a.model or r["model"] == a.model)]
    summarize(rows)
    if a.markdown:
        Path(a.markdown).write_text(markdown(rows), encoding="utf-8")
        print(f"\nwrote {a.markdown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
