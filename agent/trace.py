"""Step / Run records and persistence to runs/traces.jsonl (one run per line).

The same schema is used for every variant so baseline and guarded runs can
be compared field by field. `guard_events` is always present (empty for the
baseline) for that reason.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

TRACE_SCHEMA_VERSION = 1


@dataclass
class Step:
    step: int  # 1-based, global order within the run
    type: str  # "model_call" | "tool_call"
    iteration: int  # which model call this step belongs to (1-based)
    latency_ms: float
    # model_call fields
    text: str | None = None
    requested_tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None  # included in output_tokens
    reasoning_text: str | None = None  # model's reasoning, if the provider returns it (not sent back to it)
    cost_usd: float | None = None
    cost_source: str | None = None
    response_model: str | None = None
    attempts: int | None = None  # model calls only: 1 = no retry
    retry_errors: list[str] | None = None
    retry_overhead_ms: float | None = None
    # tool_call fields
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: Any = None  # parsed arguments as executed
    tool_arguments_raw: str | None = None  # exactly what the model sent
    tool_result: Any = None
    tool_error: str | None = None


@dataclass
class RunTrace:
    run_id: str
    timestamp: str  # UTC ISO-8601, run start
    variant: str
    model: str
    service: str
    requester_email: str
    documents: list[dict[str, Any]]  # Document.summary() for each upload (incl. all extracted text)
    config: dict[str, Any]  # max_steps, sampling params, prompt key + sha256, model capabilities, ...
    steps: list[Step] = field(default_factory=list)
    guard_events: list[dict[str, Any]] = field(default_factory=list)
    # outcome
    status: str = "running"  # "completed" | "truncated" (final reply cut by max_tokens) | "hit_max_steps" | "error"
    decision: str | None = None
    final_output: Any = None  # parsed final JSON (as-is, unvalidated)
    final_text_raw: str | None = None
    parse_error: bool = False
    final_fenced: bool = False  # final reply was wrapped in a Markdown code block (unwrapped before parsing)
    hit_max_steps: bool = False
    error: str | None = None
    # metrics
    metrics: dict[str, Any] = field(default_factory=dict)
    side_effects: dict[str, Any] = field(default_factory=dict)
    schema_version: int = TRACE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def compute_metrics(self, wall_ms: float) -> dict[str, Any]:
        model_steps = [s for s in self.steps if s.type == "model_call"]
        tool_steps = [s for s in self.steps if s.type == "tool_call"]
        costs = [s.cost_usd for s in model_steps]
        known = [c for c in costs if c is not None]
        by_tool: dict[str, int] = {}
        for s in tool_steps:
            by_tool[s.tool_name or "?"] = by_tool.get(s.tool_name or "?", 0) + 1
        self.metrics = {
            "input_tokens": sum(s.input_tokens or 0 for s in model_steps),
            "output_tokens": sum(s.output_tokens or 0 for s in model_steps),
            "reasoning_tokens": sum(s.reasoning_tokens or 0 for s in model_steps),  # subset of output_tokens
            "total_tokens": sum((s.input_tokens or 0) + (s.output_tokens or 0) for s in model_steps),
            # no model calls -> 0.0; some calls but no known cost -> None (unknown, not free)
            "total_cost_usd": float(sum(known)) if (known or not costs) else None,
            # False if any model call's cost could not be determined (then total is a lower bound)
            "cost_complete": len(known) == len(costs),
            "total_latency_ms": wall_ms,  # wall clock for the whole run, incl. document loading
            "model_latency_ms": sum(s.latency_ms for s in model_steps),  # successful calls only
            "num_retries": sum((s.attempts or 1) - 1 for s in model_steps),
            "retry_overhead_ms": sum(s.retry_overhead_ms or 0 for s in model_steps),
            "tool_latency_ms": sum(s.latency_ms for s in tool_steps),
            "num_steps": len(self.steps),
            "num_model_calls": len(model_steps),
            "num_tool_calls": len(tool_steps),
            "num_tool_errors": sum(1 for s in tool_steps if s.tool_error),
            "tool_calls_by_name": by_tool,
        }
        return self.metrics


def summarize_side_effects(rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    counts = {t: len(r) for t, r in rows.items()}
    return {"counts": counts, "total": sum(counts.values()), "rows": rows}


def append_trace(path: str | Path, trace: RunTrace) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(trace.to_dict(), ensure_ascii=False, default=str) + "\n"
    # One write per run, O_APPEND: safe enough for parallel runs on one machine.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def read_traces(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
