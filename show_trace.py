"""Show one run from runs/traces.jsonl in readable form: what the model thought,
which tools it called, what came back, what was actually written.

    python show_trace.py            # the latest run
    python show_trace.py <run_id>   # a specific run (prefix is enough)
    python show_trace.py --list     # list runs
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):  # Arabic output on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from agent.trace import read_traces  # noqa: E402
from config import get_settings  # noqa: E402


def short(v, n=160):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + " ..."


def show(t: dict) -> None:
    print(f"run {t['run_id']}  {t['timestamp'][:19]}  variant={t['variant']}  model={t['model']}")
    print(f"service={t['service']}  requester={t['requester_email']}  today={t['config'].get('today')}")
    for i, d in enumerate(t["documents"], 1):
        p = (d.get("pages") or [{}])[0]
        print(f"  document {i}: {Path(d['path']).name}  ({d.get('kind')}, text from {p.get('text_source')}, {p.get('text_chars')} chars)")
    print()
    for s in t["steps"]:
        if s["type"] == "model_call":
            retry = f"  (after {s['attempts'] - 1} retries: {s['retry_errors'][-1][:60]})" if (s.get("attempts") or 1) > 1 else ""
            print(f"[{s['step']}] MODEL  {s['input_tokens']}->{s['output_tokens']} tokens, {s['latency_ms'] / 1000:.1f}s{retry}")
            if s.get("reasoning_text"):
                print(textwrap.indent(textwrap.fill(short(s["reasoning_text"], 600), 110), "      thinks: "))
            if s.get("requested_tool_calls"):
                print("      asks for: " + ", ".join(c["name"] for c in s["requested_tool_calls"]))
        else:
            print(f"[{s['step']}] TOOL   {s['tool_name']}({short(s['tool_arguments'], 120)})")
            print(f"      -> {short(s['tool_error'] or s['tool_result'])}")
    print(f"\nstatus={t['status']}  decision={t['decision']}  parse_error={t['parse_error']}  hit_max_steps={t['hit_max_steps']}")
    if t.get("error"):
        print("error:", t["error"].splitlines()[0])
    if t.get("final_output"):
        fo = t["final_output"]
        print("reason:", fo.get("reason") if isinstance(fo, dict) else fo)
    se = t["side_effects"]
    print(f"\nwhat actually happened: {se['counts']}")
    for m in se["rows"].get("outbox", []):
        print(f"  email -> {m['to_address']}: {m['subject']}")
    for r in se["rows"].get("masters_registrations", []):
        print(f"  masters registration {r['id']}: {r['full_name']}, {r['major']}")
    for r in se["rows"].get("graduation_notices", []):
        print(f"  graduation notice {r['id']}: {r['full_name']}, {r['major']}, {r['graduation_date']} -> {r['pdf_path']}")
    m = t["metrics"]
    cost = f"${m['total_cost_usd']:.4f}" if m.get("total_cost_usd") is not None else "unknown"
    print(f"\nmetrics: {m['num_model_calls']} model calls, {m['num_tool_calls']} tool calls, "
          f"{m['total_tokens']} tokens ({m.get('reasoning_tokens', 0)} reasoning), {m['total_latency_ms'] / 1000:.1f}s, cost {cost}")


def main() -> int:
    traces = read_traces(get_settings().traces_path)
    if not traces:
        print("no runs yet")
        return 1
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        for t in traces:
            print(f"{t['run_id'][:12]}  {t['timestamp'][:19]}  {t['service']:<20} {str(t['decision']):<13} {t['status']}")
        return 0
    if len(sys.argv) > 1:
        match = [t for t in traces if t["run_id"].startswith(sys.argv[1])]
        if not match:
            print("no such run")
            return 1
        show(match[-1])
    else:
        show(traces[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
