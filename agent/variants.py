"""Variants: what differs between the versions of the agent.

A variant = a system prompt + a tool set + a Guardrails object + who approves actions.
The loop calls the Guardrails hooks at fixed points. In the baseline every hook is a
pass-through, so the baseline loop is exactly: model decides, tools execute as asked,
final JSON is taken as-is.

| variant            | prompt      | guardrails in code                       | human approval      |
|--------------------|-------------|------------------------------------------|---------------------|
| baseline           | baseline    | none                                     | no                  |
| prompt_only        | prompt_only | none (a stricter prompt is the only one) | no                  |
| tools_only         | baseline    | deterministic, document-level rules off  | no                  |
| guarded            | guarded     | deterministic, everything on             | no                  |
| prompt_only_human  | prompt_only | none                                     | every action waits  |
| guarded_human      | guarded     | deterministic, everything on             | every action waits  |

"Human approval" (guardrail G16): send_email, register_masters and issue_graduation_notice
are not performed by the agent; they are stored, in order, and a staff member approves
(they are performed then) or rejects them (agent/approvals.py, web UI staff tab).
The variant decides it, not the environment: the key alone says how a run was made.
Only one refinement comes from the environment: HUMAN_APPROVAL=flagged makes
guarded_human send only warning cases to staff (clean cases run automatically).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agent.documents import Document
from agent.tools import TOOL_FUNCTIONS, ToolContext, build_tool_schemas

ACTION_TOOLS = {"register_masters", "issue_graduation_notice", "send_email"}


class Guardrails:
    """Hook points. Every default is a no-op. Interventions go to self.events -> trace.guard_events.

    The factory is called with keyword arguments describing the run (service,
    requester_email, today, reference, state, ocr settings...). The base class
    accepts and ignores them, so the baseline is unaffected.
    """

    name = "none"

    def __init__(self, **_: Any) -> None:
        self.events: list[dict[str, Any]] = []
        self.current_step: int = 0  # set by the loop before each hook call (for event bookkeeping)

    def transform_documents(self, documents: list[Document]) -> list[Document]:
        """Before the first model call. E.g. drop hidden text, decode QR codes."""
        return documents

    def intercept_tool_call(self, name: str, arguments_raw: str, ctx: ToolContext) -> dict[str, Any] | None:
        """Before each tool executes. Return None to execute; return a dict to skip
        execution and give the model that dict as the tool result instead."""
        return None

    def finalize(self, decision: str | None, final_output: Any, ctx: ToolContext) -> str | None:
        """After the loop ends. May override the decision (e.g. deterministic verdict)."""
        return decision

    def summary(self) -> dict[str, Any]:
        """Recorded as trace.guard_summary. Empty for the baseline."""
        return {}


class HumanApproval(Guardrails):
    """G16 without guardrails: every action the agent asks for waits for a staff member.

    Wraps another Guardrails object (here: the no-op one of prompt_only). The inner object is
    asked first; whatever it lets through, if it is an action, is stored for a staff member
    instead of being performed. The model is told the action is waiting.
    """

    def __init__(self, inner: Guardrails, **_: Any) -> None:
        self.inner = inner
        self.events = inner.events  # one event list for the trace
        self.pending: list[dict[str, Any]] = []
        self.name = f"{inner.name}+human_approval"

    @property
    def current_step(self) -> int:  # type: ignore[override]
        return self.inner.current_step

    @current_step.setter
    def current_step(self, value: int) -> None:
        self.inner.current_step = value

    def transform_documents(self, documents: list[Document]) -> list[Document]:
        return self.inner.transform_documents(documents)

    def intercept_tool_call(self, name: str, arguments_raw: str, ctx: ToolContext) -> dict[str, Any] | None:
        inner = self.inner.intercept_tool_call(name, arguments_raw, ctx)
        if inner is not None or name not in ACTION_TOOLS:
            return inner
        import json

        try:
            args = json.loads(arguments_raw) if arguments_raw.strip() else {}
        except json.JSONDecodeError:
            args = {"_raw": arguments_raw}  # stored as given; the staff member sees it
        if not isinstance(args, dict):
            args = {"_raw": args}
        pid = ctx.state.add_pending_action(ctx.run_id, len(self.pending) + 1, name, args, "every_action")
        self.pending.append({"id": pid, "tool": name})
        self.events.append({"step": self.current_step, "guard": "G16", "action": "queued",
                            "tool": name, "reason": "waiting_for_staff_approval",
                            "details": {"pending_action_id": pid, "why": "every_action"}})
        out: dict[str, Any] = {"status": "queued_for_staff_approval", "pending_action_id": pid,
                               "note": "A staff member must approve this before it happens. Do not repeat it."}
        if name == "issue_graduation_notice":
            out["attachment_id"] = f"pending:{pid}"
        return out

    def finalize(self, decision: str | None, final_output: Any, ctx: ToolContext) -> str | None:
        # no rules here: the AI's answer is only a recommendation; a staff member decides
        self.model_decision = self.inner.finalize(decision, final_output, ctx)
        self.events.append({"step": self.current_step, "guard": "G16", "action": "flagged",
                            "tool": None, "reason": "final_decision_by_staff",
                            "details": {"recommendation": None, "model_decision": self.model_decision}})
        return "needs_review"

    def summary(self) -> dict[str, Any]:
        return {**self.inner.summary(), "human_approval": "actions", "awaiting_staff": True,
                "model_decision": getattr(self, "model_decision", None),
                "pending_actions": [p["id"] for p in self.pending]}


@dataclass
class VariantSpec:
    name: str
    prompt_key: str
    guardrails_factory: Callable[..., Guardrails] = Guardrails
    tool_functions: dict[str, Callable[..., Any]] = field(default_factory=lambda: dict(TOOL_FUNCTIONS))
    schema_builder: Callable[..., list[dict[str, Any]]] = build_tool_schemas
    human_approval: str = "off"  # "off" | "actions" (see the module docstring)


def _deterministic(**kwargs: Any) -> Guardrails:
    from agent.guards import DeterministicGuardrails  # imported here: guards.py imports this module

    return DeterministicGuardrails(**kwargs)


def _tools_only(**kwargs: Any) -> Guardrails:
    from agent.guards import DeterministicGuardrails

    return DeterministicGuardrails(document_rules=False, **kwargs)


def _prompt_only_human(**kwargs: Any) -> Guardrails:
    return HumanApproval(Guardrails(**kwargs))


VARIANTS: dict[str, VariantSpec] = {
    "baseline": VariantSpec(name="baseline", prompt_key="baseline"),
    "prompt_only": VariantSpec(name="prompt_only", prompt_key="prompt_only"),
    "tools_only": VariantSpec(name="tools_only", prompt_key="baseline", guardrails_factory=_tools_only),
    "guarded": VariantSpec(name="guarded", prompt_key="guarded", guardrails_factory=_deterministic),
    "prompt_only_human": VariantSpec(name="prompt_only_human", prompt_key="prompt_only",
                                     guardrails_factory=_prompt_only_human, human_approval="actions"),
    "guarded_human": VariantSpec(name="guarded_human", prompt_key="guarded",
                                 guardrails_factory=_deterministic, human_approval="actions"),
}


def human_approval_mode(spec: VariantSpec, setting: str) -> str:
    """The approval mode a run actually uses: the variant's, refined to "flagged" for guarded_human
    when HUMAN_APPROVAL=flagged (without guardrails there are no warnings to decide by)."""
    if spec.human_approval == "off":
        return "off"
    if setting == "flagged" and spec.guardrails_factory is _deterministic:
        return "flagged"
    return spec.human_approval


def get_variant(name: str) -> VariantSpec:
    try:
        return VARIANTS[name]
    except KeyError:
        raise KeyError(f"Unknown variant {name!r}. Known: {sorted(VARIANTS)}") from None
