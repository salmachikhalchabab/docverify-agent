"""run_agent(): the single entry point for the CLI, the UI, the evaluation and the guarded variant.

    result = run_agent(service="transcript", requester_email="...",
                       documents=["path/to/id.pdf"], variant="baseline")
    result.decision, result.trace, result.metrics, result.side_effects

Every run (including failed ones) is appended to runs/traces.jsonl.
"""

from __future__ import annotations

import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from agent.datastore import (
    DataNotConfiguredError,
    ReferenceStore,
    SQLiteStateStore,
    StateStore,
    get_reference_store,
)
from agent.documents import load_document
from agent.llm import LLMClient, model_capabilities
from agent.loop import build_user_content, run_loop
from agent.prompts import get_prompt, prompt_sha256
from agent.tools import ToolContext
from agent import observability
from agent.privacy import redact_trace, secret_key
from agent.trace import RunTrace, append_trace, summarize_side_effects
from agent.variants import get_variant, human_approval_mode
from config import Settings, get_settings


@dataclass
class RunResult:
    run_id: str
    decision: str | None
    trace: RunTrace
    metrics: dict[str, Any]
    side_effects: dict[str, Any]


def make_llm(settings: Settings) -> LLMClient:
    return LLMClient(
        settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout_s=settings.llm_timeout_s,
        num_retries=settings.llm_num_retries,
        price_table=settings.price_table,
    )


def run_agent(
    service: str,
    requester_email: str,
    documents: Sequence[str | Path],
    variant: str | None = None,
    *,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    reference_store: ReferenceStore | None = None,
    state_store: StateStore | None = None,
    save: bool = True,
    today: date | None = None,
    on_step: Callable[[Any], None] | None = None,
    idempotency_scope: str = "",
    request_text: str | None = None,
) -> RunResult:
    """Run one request end to end. The keyword-only arguments are for dependency
    injection (tests, evaluation harness); normal callers omit them."""
    settings = settings or get_settings()
    spec = get_variant(variant or settings.agent_variant)
    reference = reference_store or get_reference_store(settings.reference_backend, settings.reference_db_path)
    state = state_store or SQLiteStateStore(settings.state_db_path)
    llm = llm or make_llm(settings)
    system_prompt = get_prompt(spec.prompt_key)

    # Every run tells the model a fixed "today" (config.TODAY unless the caller passes one),
    # so date checks are reproducible.
    today_str = (today or date.fromisoformat(settings.agent_today)).isoformat()
    run_id = uuid.uuid4().hex
    # The guardrails object sees the request and the stores. The baseline ignores all of it.
    # who approves actions: decided by the variant (*_human), not by .env
    approval = human_approval_mode(spec, settings.human_approval)
    guardrails = spec.guardrails_factory(
        service=service,
        requester_email=requester_email,
        request_text=request_text,
        today=today_str,
        reference=reference,
        state=state,
        ocr_engine=settings.ocr_engine,
        ocr_lang=settings.ocr_lang,
        hidden_text_threshold=settings.hidden_text_threshold,
        human_approval=approval,
        idempotency_scope=idempotency_scope,
    )
    t0 = time.perf_counter()
    trace = RunTrace(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        variant=spec.name,
        model=llm.model,
        service=service,
        requester_email=requester_email,
        request_text=request_text,
        documents=[{"path": str(p)} for p in documents],
        config={
            "today": today_str,
            "max_steps": settings.max_steps,
            "llm_params": llm.call_params(),
            "prompt_key": spec.prompt_key,
            "prompt_sha256": prompt_sha256(system_prompt),
            "guardrails": guardrails.name,
            "model_capabilities": model_capabilities(llm.model),
            "page_render_dpi": settings.page_render_dpi,
            "max_image_side": settings.max_image_side,
            "ocr_engine": settings.ocr_engine,
            "hidden_text_threshold": settings.hidden_text_threshold,
            "human_approval": approval,
            "trace_pii": settings.trace_pii,
            "reference_backend": settings.reference_backend or None,
        },
    )
    ctx = ToolContext(
        run_id=run_id,
        reference=reference,
        state=state,
        artifacts_dir=settings.artifacts_dir,
        notice_font=settings.notice_font_path,
        service=service,
        requester_email=requester_email,
        today=today_str,
        idempotency_scope=idempotency_scope,
    )

    try:
        # Fail fast (and loudly) if the real data isn't connected: never run against nothing.
        tool_schemas = spec.schema_builder(reference)

        docs = [
            load_document(
                p,
                dpi=settings.page_render_dpi,
                max_image_side=settings.max_image_side,
                max_pages=settings.max_pages_per_document,
                ocr_engine=settings.ocr_engine,
                ocr_lang=settings.ocr_lang,
            )
            for p in documents
        ]
        trace.documents = [d.summary() for d in docs]
        docs = guardrails.transform_documents(docs)
        ctx.documents = docs
        # What the model actually received (equal to `documents` unless a guardrail changed the text).
        trace.documents_as_sent = [d.summary() for d in docs]

        run_loop(
            trace=trace,
            llm=llm,
            system_prompt=system_prompt,
            user_content=build_user_content(service, requester_email, docs, today_str, request_text),
            tool_schemas=tool_schemas,
            tool_functions=spec.tool_functions,
            ctx=ctx,
            guardrails=guardrails,
            max_steps=settings.max_steps,
            on_step=on_step,
        )
    except DataNotConfiguredError:
        raise  # a configuration problem, not an experimental result: don't record it
    except Exception as e:  # noqa: BLE001 - record the failed run, then return it
        trace.status = "error"
        trace.error = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}"
    finally:
        trace.guard_events = list(guardrails.events)
        trace.guard_summary = guardrails.summary()
    if trace.guard_summary.get("awaiting_staff"):
        # human-in-the-loop: the request waits for a staff member's final yes or no (agent/approvals.py)
        gs = trace.guard_summary
        state.open_staff_case(run_id, service, gs.get("policy_verdict"),
                              list(gs.get("reasons") or []) + list(gs.get("flags") or []), gs.get("model_decision"))

    # Side effects are read back from the store, not taken from tool results.
    trace.side_effects = summarize_side_effects(state.side_effects(run_id))
    trace.compute_metrics(wall_ms=(time.perf_counter() - t0) * 1000.0)
    if save:
        record = trace.to_dict()
        if settings.trace_pii != "full":  # default: no document text, pseudonyms for personal data
            record = redact_trace(record, reference, secret_key(settings.runs_dir))
        append_trace(settings.traces_path, record)
        if observability.enabled():  # optional Langfuse export, always of the redacted record
            sent = record if record.get("pii") == "redacted" else redact_trace(
                trace.to_dict(), reference, secret_key(settings.runs_dir))
            observability.safe_export(sent)

    return RunResult(
        run_id=run_id,
        decision=trace.decision,
        trace=trace,
        metrics=trace.metrics,
        side_effects=trace.side_effects,
    )
