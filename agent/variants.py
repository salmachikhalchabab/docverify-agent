"""Variants: what differs between "baseline" and (later) "guarded".

A variant = a system prompt + a tool set + a Guardrails object. The loop
calls the Guardrails hooks at fixed points. In the baseline every hook is a
pass-through, so the baseline loop is exactly: model decides, tools execute
as asked, final JSON is taken as-is.

The guarded variant will subclass Guardrails (and may add tools, e.g. a QR
decoder) without changing the loop, tools, data layer or tracing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agent.documents import Document
from agent.tools import TOOL_FUNCTIONS, ToolContext, build_tool_schemas


class Guardrails:
    """Hook points. Every default is a no-op. Interventions go to self.events -> trace.guard_events."""

    name = "none"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

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


@dataclass
class VariantSpec:
    name: str
    prompt_key: str
    guardrails_factory: Callable[[], Guardrails] = Guardrails
    tool_functions: dict[str, Callable[..., Any]] = field(default_factory=lambda: dict(TOOL_FUNCTIONS))
    schema_builder: Callable[..., list[dict[str, Any]]] = build_tool_schemas


VARIANTS: dict[str, VariantSpec] = {
    "baseline": VariantSpec(name="baseline", prompt_key="baseline"),
    # "guarded": VariantSpec(name="guarded", prompt_key="guarded", guardrails_factory=DeterministicGuardrails, ...)
}


def get_variant(name: str) -> VariantSpec:
    try:
        return VARIANTS[name]
    except KeyError:
        raise KeyError(f"Unknown variant {name!r}. Known: {sorted(VARIANTS)}") from None
