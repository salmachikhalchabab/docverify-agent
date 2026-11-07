"""Optional: send every run to Langfuse (observability only; it never changes the agent).

Enabled when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set (and LANGFUSE_TRACING is not "off").
LANGFUSE_BASE_URL picks the region (default https://cloud.langfuse.com; US: https://us.cloud.langfuse.com).
LANGFUSE_TIMEOUT (seconds, default 30) is how long each request to Langfuse may take.

What is sent: the trace as saved, i.e. REDACTED (pseudonyms, no document text; see agent/privacy.py),
even when TRACE_PII=full is used locally. One Langfuse trace per run:

    run (agent)                      service, request, final answer, status, side effects, guard summary
      model #1 (generation)          model, tokens (incl. reasoning), cost, latency, tool calls requested
      lookup_registry (tool)         arguments, result, error (level ERROR), guardrail refusals (WARNING)
      ...

The trace id is derived from the run id (create_trace_id(seed=run_id)), so evaluation scores can be
attached later (score_run) and the same run never gets two trace ids.

Timing: runs are exported after they finish, with each step's REAL start and end (laid out from the
run's start time and the recorded latencies). The public SDK cannot set a start time, so this uses
the SDK's own internal span factory (pinned langfuse==5.2.0; tests/test_observability.py fails if a
new SDK version changes it). If those internals are missing, steps are still sent, without exact times.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)
_client: Any = None


def enabled() -> bool:
    if os.getenv("LANGFUSE_TRACING", "").strip().lower() in {"off", "0", "false", "no"}:
        return False
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY", "").strip() and os.getenv("LANGFUSE_SECRET_KEY", "").strip())


def client() -> Any:
    """The process-wide Langfuse client (created on first use, flushed at exit)."""
    global _client
    if _client is None:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY", "").strip(),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY", "").strip(),
            base_url=(os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or "https://cloud.langfuse.com").strip(),
            environment=os.getenv("LANGFUSE_ENVIRONMENT", "docverify").strip() or None,
            # the SDK default (5 s) is too short for slow connections: the TLS handshake alone can exceed it
            timeout=int(os.getenv("LANGFUSE_TIMEOUT") or 30),
        )
        atexit.register(flush)
    return _client


def flush() -> None:
    if _client is not None:
        try:
            _client.flush()
        except Exception as e:  # noqa: BLE001 - observability must never break a run
            log.warning("langfuse flush failed: %s", e)


def trace_id_for(run_id: str, lf: Any = None) -> str:
    from langfuse import Langfuse

    return (lf or Langfuse).create_trace_id(seed=run_id)


# --------------------------------------------------------------------------- export


def _ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)


def _jsonable(v: Any, limit: int = 20000) -> Any:
    """Keep payloads JSON-friendly and bounded (Langfuse truncates very large inputs anyway)."""
    s = json.dumps(v, ensure_ascii=False, default=str)
    return v if len(s) <= limit else s[:limit] + " …[truncated]"


class _Spans:
    """Creates observations with explicit start/end times (falls back to the public API)."""

    def __init__(self, lf: Any):
        self.lf = lf
        self.timed = all(hasattr(lf, a) for a in ("_otel_tracer", "_create_observation_from_otel_span",
                                                   "_create_remote_parent_span"))

    def root(self, trace_id: str, name: str, start_ns: int, **kw: Any) -> Any:
        if not self.timed:
            return self.lf.start_observation(trace_context={"trace_id": trace_id}, name=name, **kw)
        from opentelemetry import trace as otel_trace_api

        from langfuse._client.attributes import LangfuseOtelSpanAttributes

        parent = self.lf._create_remote_parent_span(trace_id=trace_id, parent_span_id=None)
        with otel_trace_api.use_span(parent):
            otel_span = self.lf._otel_tracer.start_span(name=name, start_time=start_ns)
        otel_span.set_attribute(LangfuseOtelSpanAttributes.AS_ROOT, True)
        return self.lf._create_observation_from_otel_span(otel_span=otel_span, **kw)

    def child(self, parent: Any, name: str, start_ns: int, **kw: Any) -> Any:
        if not self.timed:
            return parent.start_observation(name=name, **kw)
        from opentelemetry import trace as otel_trace_api

        ctx = otel_trace_api.set_span_in_context(parent._otel_span)
        otel_span = self.lf._otel_tracer.start_span(name=name, start_time=start_ns, context=ctx)
        return self.lf._create_observation_from_otel_span(otel_span=otel_span, **kw)

    def end(self, obs: Any, end_ns: int) -> None:
        obs.end(end_time=end_ns) if self.timed else obs.end()


def export_trace(record: dict[str, Any], lf: Any = None) -> str:
    """Send one saved (redacted) trace record to Langfuse. Returns the Langfuse trace id."""
    from langfuse import propagate_attributes

    lf = lf or client()
    spans = _Spans(lf)
    trace_id = trace_id_for(record["run_id"], lf)
    cfg, m = record.get("config") or {}, record.get("metrics") or {}
    start = datetime.fromisoformat(record["timestamp"]).astimezone(timezone.utc)
    start_ns = _ns(start)
    total_ns = int(float(m.get("total_latency_ms") or 0) * 1e6)
    steps = record.get("steps") or []

    def dur_ns(s: dict[str, Any]) -> int:
        return int((float(s.get("latency_ms") or 0) + float(s.get("retry_overhead_ms") or 0)) * 1e6)

    # document loading and pre-model checks happen first; the steps follow back to back
    cursor = start_ns + max(0, total_ns - sum(dur_ns(s) for s in steps))
    events_by_step: dict[int, list[dict[str, Any]]] = {}
    for e in record.get("guard_events") or []:
        events_by_step.setdefault(int(e.get("step") or 0), []).append(e)

    model_short = str(record.get("model", "")).split("/")[-1]
    tags = [f"variant:{record.get('variant')}", f"model:{model_short}", f"service:{record.get('service')}",
            f"status:{record.get('status')}", f"decision:{record.get('decision')}",
            f"prompt:{str(cfg.get('prompt_sha256') or '')[:8]}"]
    meta = {"run_id": record["run_id"], "variant": record.get("variant"), "model": record.get("model"),
            "prompt_sha256": cfg.get("prompt_sha256"), "today": cfg.get("today"), "pii": record.get("pii")}
    with propagate_attributes(trace_name=f"{record.get('service')} ({record.get('variant')})", tags=tags,
                              metadata={k: str(v) for k, v in meta.items() if v is not None}):
        root = spans.root(
            trace_id, "run", start_ns, as_type="agent",
            input=_jsonable({"service": record.get("service"), "requester_email": record.get("requester_email"),
                             "request_text": record.get("request_text"),
                             "documents": [d.get("name") or d.get("path") for d in record.get("documents") or []]}),
            output=_jsonable(record.get("final_output") if record.get("final_output") is not None
                             else record.get("final_text_raw")),
            metadata=_jsonable({"status": record.get("status"), "decision": record.get("decision"),
                                "parse_error": record.get("parse_error"), "hit_max_steps": record.get("hit_max_steps"),
                                "error": record.get("error"), "side_effects": (record.get("side_effects") or {}).get("counts"),
                                "guard_summary": record.get("guard_summary"), "metrics": m}),
            level="ERROR" if record.get("status") == "error" else None,
            status_message=(record.get("error") or "")[:500] or None,
        )
        for s in steps:
            d = dur_ns(s)
            events = events_by_step.get(int(s.get("step") or 0), [])
            if s.get("type") == "model_call":
                obs = spans.child(
                    root, f"model #{s.get('iteration')}", cursor, as_type="generation",
                    model=s.get("response_model") or record.get("model"),
                    model_parameters={k: v for k, v in (cfg.get("llm_params") or {}).items()},
                    output=_jsonable({"text": s.get("text"), "tool_calls": s.get("requested_tool_calls")}),
                    usage_details={k: int(v) for k, v in {"input": s.get("input_tokens"), "output": s.get("output_tokens"),
                                                         "reasoning": s.get("reasoning_tokens")}.items() if v is not None},
                    cost_details={"total": float(s["cost_usd"])} if s.get("cost_usd") is not None else None,
                    metadata=_jsonable({"step": s.get("step"), "finish_reason": s.get("finish_reason"),
                                        "attempts": s.get("attempts"), "retry_errors": s.get("retry_errors"),
                                        "reasoning": s.get("reasoning_text")}),
                )
            else:
                blocked = any(e.get("action") == "blocked" for e in events)
                obs = spans.child(
                    root, str(s.get("tool_name")), cursor, as_type="tool",
                    input=_jsonable(s.get("tool_arguments") if s.get("tool_arguments") is not None else s.get("tool_arguments_raw")),
                    output=_jsonable(s.get("tool_result")),
                    metadata=_jsonable({"step": s.get("step"), "guard_events": events or None}),
                    level="ERROR" if s.get("tool_error") else ("WARNING" if blocked else None),
                    status_message=(s.get("tool_error") or "")[:500] or None,
                )
            spans.end(obs, cursor + d)
            cursor += d
        spans.end(root, start_ns + max(total_ns, cursor - start_ns))
    return trace_id


def score_run(run_id: str, scores: dict[str, Any], lf: Any = None) -> None:
    """Attach evaluation results to a run's trace (booleans as 0/1 BOOLEAN, numbers as NUMERIC)."""
    lf = lf or client()
    tid = trace_id_for(run_id, lf)
    for name, value in scores.items():
        if value is None:
            continue
        if isinstance(value, bool):
            lf.create_score(trace_id=tid, name=name, value=float(value), data_type="BOOLEAN")
        elif isinstance(value, (int, float)):
            lf.create_score(trace_id=tid, name=name, value=float(value), data_type="NUMERIC")
        else:
            lf.create_score(trace_id=tid, name=name, value=str(value), data_type="CATEGORICAL")


def safe_export(record: dict[str, Any]) -> None:
    """export_trace that can never break a run (errors are logged)."""
    if not enabled():
        return
    try:
        export_trace(record)
    except Exception as e:  # noqa: BLE001
        log.warning("langfuse export failed for run %s: %s", record.get("run_id"), e)


def safe_score(run_id: str, scores: dict[str, Any]) -> None:
    if not enabled():
        return
    try:
        score_run(run_id, scores)
    except Exception as e:  # noqa: BLE001
        log.warning("langfuse scoring failed for run %s: %s", run_id, e)
