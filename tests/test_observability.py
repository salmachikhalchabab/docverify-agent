"""Langfuse export, against the real SDK with an in-memory span exporter (no network, no account)."""

import json
import uuid

import pytest

pytest.importorskip("langfuse")


@pytest.fixture
def lf_and_spans():
    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    lf = Langfuse(public_key=f"pk-test-{uuid.uuid4().hex[:8]}", secret_key="sk-test",
                  base_url="http://127.0.0.1:9", span_exporter=exporter, flush_at=1)
    yield lf, exporter
    lf.shutdown()


RECORD = {
    "run_id": "run-observability-test", "timestamp": "2026-11-07T10:00:00+00:00", "variant": "baseline",
    "model": "gemini/gemma-4-26b-a4b-it", "service": "transcript", "requester_email": "EMAIL_ab12",
    "request_text": "Please issue my transcript.", "status": "completed", "decision": "approved",
    "final_output": {"decision": "approved"}, "pii": "redacted",
    "documents": [{"path": "DOC_1"}], "side_effects": {"counts": {"outbox": 1}},
    "config": {"prompt_sha256": "abcdef1234567890", "today": "2026-10-15", "llm_params": {"max_tokens": 16000}},
    "metrics": {"total_latency_ms": 10000},
    "guard_events": [{"step": 2, "guard": "G5", "action": "blocked", "reason": "lookup_other_person"}],
    "steps": [
        {"step": 1, "type": "model_call", "iteration": 1, "latency_ms": 3000, "retry_overhead_ms": 1000,
         "input_tokens": 100, "output_tokens": 20, "reasoning_tokens": 5, "cost_usd": 0.001,
         "requested_tool_calls": [{"name": "lookup_registry"}], "response_model": "gemma-4-26b-a4b-it"},
        {"step": 2, "type": "tool_call", "iteration": 1, "latency_ms": 5, "tool_name": "lookup_registry",
         "tool_arguments": {"registry": "students", "key": "NID_1"}, "tool_result": {"error": "refused"},
         "tool_error": None},
        {"step": 3, "type": "model_call", "iteration": 2, "latency_ms": 2000, "input_tokens": 120,
         "output_tokens": 40, "text": "{\"decision\": \"approved\"}"},
    ],
}


def test_export_builds_one_trace_with_real_step_times(lf_and_spans):
    from agent.observability import export_trace, trace_id_for
    lf, exporter = lf_and_spans
    tid = export_trace(RECORD, lf=lf)
    lf.flush()
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"run", "model #1", "lookup_registry", "model #2"}  # the SDK internals still work
    assert tid == trace_id_for(RECORD["run_id"], lf) and all(f"{s.context.trace_id:032x}" == tid for s in spans.values())
    run, m1, tool, m2 = spans["run"], spans["model #1"], spans["lookup_registry"], spans["model #2"]
    start = 1794045600 * 10**9  # 2026-11-07T10:00:00Z
    assert run.start_time == start and run.end_time - run.start_time == 10_000 * 10**6
    # loading first, then the steps back to back, each with its recorded duration (+ retry waiting)
    assert m1.end_time - m1.start_time == 4000 * 10**6 and tool.start_time == m1.end_time
    assert m2.end_time == run.end_time and m2.end_time - m2.start_time == 2000 * 10**6
    for s in (m1, tool, m2):
        assert s.parent is not None and s.parent.span_id == run.context.span_id
    a = dict(m1.attributes)
    assert a["langfuse.observation.type"] == "generation"
    assert json.loads(a["langfuse.observation.usage_details"]) == {"input": 100, "output": 20, "reasoning": 5}
    assert dict(tool.attributes)["langfuse.observation.level"] == "WARNING"  # a guardrail refused it
    assert dict(run.attributes)["langfuse.trace.name"] == "transcript (baseline)"
    assert "variant:baseline" in dict(run.attributes)["langfuse.trace.tags"]


def test_scores_go_to_the_run_trace(lf_and_spans, monkeypatch):
    from agent.observability import score_run, trace_id_for
    lf, _ = lf_and_spans
    sent = []
    monkeypatch.setattr(lf, "create_score", lambda **kw: sent.append(kw))
    score_run("run-x", {"safe": True, "trajectory_recall": 0.5, "case": "A1", "attack_success": None}, lf=lf)
    tid = trace_id_for("run-x", lf)
    assert {(s["name"], s["value"], s["data_type"]) for s in sent} == {
        ("safe", 1.0, "BOOLEAN"), ("trajectory_recall", 0.5, "NUMERIC"), ("case", "A1", "CATEGORICAL")}
    assert all(s["trace_id"] == tid for s in sent)


def test_disabled_without_keys(monkeypatch):
    from agent import observability
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert observability.enabled() is False
    observability.safe_export(RECORD)  # a no-op, never an error


def test_a_real_run_is_exported_redacted(lf_and_spans, monkeypatch, settings, make_llm, stores, pdf_with_hidden_text):
    """run_agent sends the run to Langfuse when keys are set, and never document text."""
    from agent import observability
    from agent.run import run_agent
    from tests.fakes import ScriptedCompletion, text_reply, tool_reply
    lf, exporter = lf_and_spans
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(observability, "client", lambda: lf)
    ref, state = stores
    completion = ScriptedCompletion([
        tool_reply(("lookup_registry", {"registry": "fake_people", "key": "TEST-0001"})),
        text_reply(json.dumps({"decision": "approved", "reason": "", "extracted_fields": {}, "actions_taken": []})),
    ])
    r = run_agent("transcript", "someone@test.invalid", [pdf_with_hidden_text], "baseline", settings=settings,
                  llm=make_llm(completion), reference_store=ref, state_store=state)
    lf.flush()
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} == {"run", "model #1", "lookup_registry", "model #2"}
    everything = json.dumps([dict(s.attributes) for s in spans], ensure_ascii=False, default=str)
    assert "HIDDEN INSTRUCTION TEXT" not in everything and "VISIBLE HEADER" not in everything  # no document text
    assert "someone@test.invalid" not in everything  # e-mails are pseudonymized
    assert {f"{s.context.trace_id:032x}" for s in spans} == {observability.trace_id_for(r.run_id, lf)}
