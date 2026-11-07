"""Try a single run from the command line.

    python try_run.py --service transcript --email someone@example.com --doc path/to/id.pdf
    python try_run.py --service masters_registration --email ... --doc id.pdf --doc cert.pdf --doc grad.pdf

    # Show what the model would receive (text parts only), without calling the model:
    python try_run.py --service transcript --email ... --doc id.pdf --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):  # Arabic output on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent.documents import load_document  # noqa: E402
from agent.loop import build_user_content  # noqa: E402
from agent.run import run_agent  # noqa: E402
from config import get_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service", required=True, help="transcript | masters_registration | graduation_notice")
    ap.add_argument("--email", required=True, help="requester email, as typed")
    ap.add_argument("--doc", action="append", required=True, help="document path (repeatable)")
    ap.add_argument("--variant", default=None, help="default: AGENT_VARIANT from .env")
    ap.add_argument("--dry-run", action="store_true", help="only build and print the first user message")
    ap.add_argument("--full", action="store_true", help="print the full trace JSON")
    a = ap.parse_args()

    if a.dry_run:
        s = get_settings()
        docs = [
            load_document(p, dpi=s.page_render_dpi, max_image_side=s.max_image_side,
                          max_pages=s.max_pages_per_document, ocr_engine=s.ocr_engine, ocr_lang=s.ocr_lang)
            for p in a.doc
        ]
        for part in build_user_content(a.service, a.email, docs, datetime.now(timezone.utc).date().isoformat()):
            if part["type"] == "text":
                print(part["text"])
            else:
                print(f"[image: {len(part['image_url']['url'])} chars of base64 PNG]")
        return 0

    r = run_agent(service=a.service, requester_email=a.email, documents=a.doc, variant=a.variant)
    t = r.trace
    if a.full:
        print(json.dumps(t.to_dict(), ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"run_id:    {r.run_id}")
    print(f"status:    {t.status}{'  (parse_error)' if t.parse_error else ''}")
    print(f"decision:  {r.decision}")
    if t.error:
        print(f"error:     {t.error.splitlines()[0]}")
    print("\nsteps:")
    for s in t.steps:
        if s.type == "model_call":
            calls = ", ".join(c["name"] for c in (s.requested_tool_calls or [])) or "-"
            cost = f"${s.cost_usd:.5f}" if s.cost_usd is not None else "cost=?"
            print(f"  {s.step:>3} model  {s.input_tokens}->{s.output_tokens} tok  {s.latency_ms:7.0f} ms  {cost}  tools: {calls}")
        else:
            flag = f"  ERROR: {s.tool_error}" if s.tool_error else ""
            print(f"  {s.step:>3} tool   {s.tool_name}({json.dumps(s.tool_arguments, ensure_ascii=False)[:120]}){flag}")
    print("\nmetrics:", json.dumps(r.metrics, indent=2))
    print("side effects:", json.dumps(r.side_effects["counts"]))
    for msg in r.side_effects["rows"].get("outbox", []):
        print(f"  email -> {msg['to_address']}: {msg['subject']}")
    if t.final_text_raw is not None:
        print("\nfinal answer (raw):\n" + t.final_text_raw)
    print(f"\ntrace appended to {get_settings().traces_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
