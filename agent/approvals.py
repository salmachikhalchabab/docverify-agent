"""Human approval (guardrail G16): a staff member decides on the actions a guarded run queued.

With HUMAN_APPROVAL=actions (or =flagged for warning cases) the guarded variant does not perform
send_email / register_masters / issue_graduation_notice itself. It stores each call, in order, in
the state database (table pending_actions) and tells the model the action is waiting.

A staff member then reviews the run (documents, policy verdict, reasons) and either
  - approves it: the stored calls are performed now, in their original order, by the same tool
    functions the agent uses, and recorded under the original run_id; or
  - rejects it: nothing is performed.
Every decision keeps who decided and when (reviewer, reviewed_at). Nothing here asks a model.
"""

from __future__ import annotations

from typing import Any

from agent.datastore import ReferenceStore, StateStore
from agent.tools import TOOL_FUNCTIONS, ToolContext
from config import Settings


def pending_runs(state: StateStore) -> dict[str, list[dict[str, Any]]]:
    """Runs waiting for a staff decision -> their waiting actions (possibly none), oldest first.
    A run waits if it has an open staff case (human-in-the-loop versions) or a waiting action."""
    out: dict[str, list[dict[str, Any]]] = {c["run_id"]: [] for c in state.staff_cases(status="pending")}
    for a in state.pending_actions(status="pending"):
        out.setdefault(a["run_id"], []).append(a)
    return out


def review_run(run_id: str, approve: bool, reviewer: str, *, settings: Settings, reference: ReferenceStore,
               state: StateStore, note: str = "") -> list[dict[str, Any]]:
    """The staff member's final decision on one run: approve (perform every waiting action, in order)
    or reject (perform none). Closes the run's staff case. Returns what happened per action."""
    if not reviewer.strip():
        raise ValueError("a reviewer name is required")
    actions = sorted(state.pending_actions(run_id=run_id, status="pending"), key=lambda a: a["seq"])
    case = state.staff_case(run_id)
    if not actions and (case is None or case["status"] != "pending"):
        raise LookupError(f"nothing waits for a decision in run {run_id}")
    if case is not None and case["status"] == "pending":
        state.close_staff_case(run_id, "approved" if approve else "rejected", reviewer, note)
    if not approve:
        for a in actions:
            state.set_pending_status(a["id"], "rejected", reviewer, {"status": "not performed"})
        return [{"id": a["id"], "tool": a["tool"], "status": "rejected"} for a in actions]

    ctx = ToolContext(run_id=run_id, reference=reference, state=state, artifacts_dir=settings.artifacts_dir,
                      notice_font=settings.notice_font_path)
    performed: dict[str, str] = {}  # pending id of a notice -> the real attachment id
    out = []
    for a in actions:
        args = dict(a["arguments"])
        att = args.get("attachment_id")
        if isinstance(att, str) and att.startswith("pending:"):
            real = performed.get(att[len("pending:"):])
            if real is None:  # the notice it refers to was not performed: do not send a broken email
                state.set_pending_status(a["id"], "rejected", reviewer, {"error": "attachment was not issued"})
                out.append({"id": a["id"], "tool": a["tool"], "status": "skipped"})
                continue
            args["attachment_id"] = real
        result = TOOL_FUNCTIONS[a["tool"]](ctx, **args)
        if a["tool"] == "issue_graduation_notice":
            performed[a["id"]] = result["attachment_id"]
        state.set_pending_status(a["id"], "approved", reviewer, result)
        out.append({"id": a["id"], "tool": a["tool"], "status": "performed", "result": result})
    return out
