"""End-to-end run_agent() tests with a scripted (fake) model. Verifies loop + tracing, not model behavior."""

import json

import pytest

from agent.datastore import DataNotConfiguredError, UnconfiguredReferenceStore
from agent.run import run_agent
from agent.trace import read_traces
from tests.fakes import ScriptedCompletion, text_reply, tool_reply

FINAL = {"decision": "approved", "reason": "ok", "extracted_fields": {}, "actions_taken": ["emailed"]}


def _run(settings, make_llm, stores, completion, docs):
    ref, state = stores
    return run_agent(
        service="transcript", requester_email="requester@test.invalid", documents=docs, variant="baseline",
        settings=settings, llm=make_llm(completion), reference_store=ref, state_store=state,
    )


def test_happy_path_trace_metrics_and_side_effects(settings, make_llm, stores, pdf_with_hidden_text):
    completion = ScriptedCompletion([
        tool_reply(("lookup_registry", {"registry": "fake_people", "key": "TEST-0001"})),
        tool_reply(("get_transcript", {"student_id": "S-TEST-1"}),
                   ("send_email", {"to": "requester@test.invalid", "subject": "Transcript", "body": "TST101: A"})),
        text_reply(json.dumps(FINAL)),
    ])
    r = _run(settings, make_llm, stores, completion, [pdf_with_hidden_text])

    assert r.decision == "approved"
    t = r.trace
    assert t.status == "completed" and not t.parse_error and not t.hit_max_steps
    assert [s.type for s in t.steps] == ["model_call", "tool_call", "model_call", "tool_call", "tool_call", "model_call"]
    assert [s.step for s in t.steps] == [1, 2, 3, 4, 5, 6]
    assert t.steps[1].tool_result["found"] is True

    m = r.metrics
    assert m["num_model_calls"] == 3 and m["num_tool_calls"] == 3
    assert m["input_tokens"] == 3200 and m["output_tokens"] == 180
    assert m["total_cost_usd"] == pytest.approx(3200 / 1e6 * 3 + 180 / 1e6 * 15)
    assert m["cost_complete"] is True
    assert all(s.cost_source == "price_table" for s in t.steps if s.type == "model_call")
    assert m["tool_calls_by_name"] == {"lookup_registry": 1, "get_transcript": 1, "send_email": 1}

    assert r.side_effects["counts"] == {"outbox": 1, "masters_registrations": 0, "graduation_notices": 0}

    # tool results were fed back to the model; system prompt was the baseline one
    last_msgs = completion.calls[-1]["messages"]
    assert last_msgs[0]["role"] == "system" and "Student Affairs Assistant" in last_msgs[0]["content"]
    assert sum(m["role"] == "tool" for m in last_msgs) == 3
    assert completion.calls[0]["tools"] and completion.calls[0]["max_tokens"] == 1000

    # persisted as exactly one JSONL line, WITHOUT document text (TRACE_PII=redact) and without image bytes
    lines = read_traces(settings.traces_path)
    assert len(lines) == 1 and lines[0]["run_id"] == r.run_id
    assert "HIDDEN INSTRUCTION TEXT" in t.documents[0]["pages"][0]["text"]  # the in-memory result has it
    assert "HIDDEN INSTRUCTION TEXT" not in settings.traces_path.read_text(encoding="utf-8")
    assert lines[0]["documents"][0]["pages"][0]["text"].startswith("[not stored:") and lines[0]["pii"] == "redacted"
    assert "base64" not in settings.traces_path.read_text()
    assert lines[0]["config"]["prompt_sha256"] and lines[0]["guard_events"] == []
    assert lines[0]["config"]["today"] == completion.calls[0]["messages"][1]["content"][0]["text"][14:24]


def test_on_step_sees_every_step_in_order(settings, make_llm, stores, pdf_with_hidden_text):
    seen = []
    ref, state = stores
    completion = ScriptedCompletion([
        tool_reply(("lookup_registry", {"registry": "fake_people", "key": "TEST-0001"})),
        text_reply(json.dumps(FINAL)),
    ])
    r = run_agent("transcript", "a@test.invalid", [pdf_with_hidden_text], "baseline", settings=settings,
                  llm=make_llm(completion), reference_store=ref, state_store=state,
                  on_step=lambda s: seen.append((s.step, s.type)))
    assert seen == [(s.step, s.type) for s in r.trace.steps] == [(1, "model_call"), (2, "tool_call"), (3, "model_call")]


def test_side_effects_recorded_even_if_model_says_rejected(settings, make_llm, stores, pdf_with_hidden_text):
    completion = ScriptedCompletion([
        tool_reply(("send_email", {"to": "someone-else@test.invalid", "subject": "x", "body": "grades"})),
        text_reply(json.dumps({**FINAL, "decision": "rejected", "actions_taken": []})),
    ])
    r = _run(settings, make_llm, stores, completion, [pdf_with_hidden_text])
    assert r.decision == "rejected"
    assert r.side_effects["counts"]["outbox"] == 1
    assert r.side_effects["rows"]["outbox"][0]["to_address"] == "someone-else@test.invalid"


def test_fenced_json_is_unwrapped_and_flagged(settings, make_llm, stores, pdf_with_hidden_text):
    raw = "```json\n" + json.dumps(FINAL) + "\n```"
    r = _run(settings, make_llm, stores, ScriptedCompletion([text_reply(raw)]), [pdf_with_hidden_text])
    assert r.trace.parse_error is False and r.trace.final_fenced is True
    assert r.decision == "approved" and r.trace.final_text_raw == raw


def test_non_json_final_is_kept_raw_with_parse_error(settings, make_llm, stores, pdf_with_hidden_text):
    raw = "Here is my answer: " + json.dumps(FINAL)  # prose around JSON is NOT repaired
    r = _run(settings, make_llm, stores, ScriptedCompletion([text_reply(raw)]), [pdf_with_hidden_text])
    assert r.trace.parse_error is True and r.trace.final_fenced is False
    assert r.decision is None
    assert r.trace.final_text_raw == raw  # no repair


def test_reply_cut_by_max_tokens_is_marked_truncated(settings, make_llm, stores, pdf_with_hidden_text):
    from litellm import ModelResponse
    cut = ModelResponse(model="test/fake-model", choices=[{"index": 0, "finish_reason": "length",
                        "message": {"role": "assistant", "content": ""}}],
                        usage={"prompt_tokens": 10, "completion_tokens": 1000, "total_tokens": 1010})
    r = _run(settings, make_llm, stores, ScriptedCompletion([cut]), [pdf_with_hidden_text])
    assert r.trace.status == "truncated" and r.trace.parse_error and r.decision is None


def test_hit_max_steps(settings, make_llm, stores, pdf_with_hidden_text):
    from dataclasses import replace
    s = replace(settings, max_steps=3)
    loop_forever = ScriptedCompletion([tool_reply(("lookup_registry", {"registry": "fake_people", "key": "x"}))], repeat_last=True)
    r = _run(s, make_llm, stores, loop_forever, [pdf_with_hidden_text])
    assert r.trace.hit_max_steps and r.trace.status == "hit_max_steps"
    assert r.decision is None
    assert r.metrics["num_model_calls"] == 3 and r.metrics["num_tool_calls"] == 3


def test_tool_error_goes_back_to_model_and_loop_continues(settings, make_llm, stores, pdf_with_hidden_text):
    completion = ScriptedCompletion([
        tool_reply(("send_email", "{not json")),
        text_reply(json.dumps({**FINAL, "decision": "needs_review"})),
    ])
    r = _run(settings, make_llm, stores, completion, [pdf_with_hidden_text])
    assert r.decision == "needs_review"
    assert r.trace.steps[1].tool_error and r.metrics["num_tool_errors"] == 1
    assert r.side_effects["total"] == 0


def test_model_api_error_is_recorded_as_failed_run(settings, make_llm, stores, pdf_with_hidden_text):
    r = _run(settings, make_llm, stores, ScriptedCompletion(raise_exc=RuntimeError("boom")), [pdf_with_hidden_text])
    assert r.trace.status == "error" and "boom" in r.trace.error
    assert read_traces(settings.traces_path)[0]["status"] == "error"


def test_transient_errors_are_retried_and_recorded(settings, stores, pdf_with_hidden_text):
    import litellm
    from agent.llm import LLMClient

    class Flaky(ScriptedCompletion):
        def __call__(self, **kw):
            if self.failures < 2:
                self.failures += 1
                raise litellm.InternalServerError(message="500 internal", llm_provider="gemini", model="m")
            return super().__call__(**kw)

    flaky = Flaky([text_reply(json.dumps(FINAL))])
    flaky.failures = 0
    llm = LLMClient("test/fake-model", num_retries=3, backoff_s=0.001, completion_fn=flaky)
    ref, state = stores
    r = run_agent("transcript", "a@test.invalid", [pdf_with_hidden_text], "baseline",
                  settings=settings, llm=llm, reference_store=ref, state_store=state)
    step = r.trace.steps[0]
    assert r.trace.status == "completed" and step.attempts == 3 and len(step.retry_errors) == 2
    assert r.metrics["num_retries"] == 2


def test_per_minute_rate_limit_waits_and_retries_but_daily_does_not():
    import litellm
    from agent.llm import LLMClient, per_minute_rate_limit_wait
    pm = litellm.BadRequestError(message='{"code": 429, "message": "Generate content input tokens per minute '
                                 'limit exceeded", "retryDelay": "37s"}', llm_provider="gemini", model="m")
    daily = litellm.RateLimitError(message="429 quota exceeded: requests per day", llm_provider="gemini", model="m")
    assert per_minute_rate_limit_wait(pm) == 39.0
    assert per_minute_rate_limit_wait(daily) is None
    calls = []

    def fn(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise pm
        return text_reply("{}")

    llm = LLMClient("test/fake-model", rate_limit_wait_scale=0.0, completion_fn=fn)
    r = llm.complete([{"role": "user", "content": "x"}], None)
    assert r.attempts == 2 and r.retry_errors[0].startswith("rate_limit_per_minute")


def test_retries_are_bounded(settings, stores, pdf_with_hidden_text):
    import litellm
    from agent.llm import LLMClient

    def always_500(**kw):
        raise litellm.InternalServerError(message="500", llm_provider="gemini", model="m")

    llm = LLMClient("test/fake-model", num_retries=2, backoff_s=0.001, completion_fn=always_500)
    ref, state = stores
    r = run_agent("transcript", "a@test.invalid", [pdf_with_hidden_text], "baseline",
                  settings=settings, llm=llm, reference_store=ref, state_store=state)
    assert r.trace.status == "error" and "InternalServerError" in r.trace.error


def test_unknown_cost_is_null_not_zero(settings, stores, pdf_with_hidden_text):
    from agent.llm import LLMClient
    llm = LLMClient("test/fake-model", completion_fn=ScriptedCompletion([text_reply(json.dumps(FINAL))]))  # no price table
    ref, state = stores
    r = run_agent("transcript", "a@test.invalid", [pdf_with_hidden_text], "baseline",
                  settings=settings, llm=llm, reference_store=ref, state_store=state)
    assert r.metrics["total_cost_usd"] is None and r.metrics["cost_complete"] is False


def test_refuses_to_run_without_real_data(settings, make_llm, pdf_with_hidden_text):
    from agent.datastore import SQLiteStateStore
    with pytest.raises(DataNotConfiguredError):
        run_agent("transcript", "a@test.invalid", [pdf_with_hidden_text], "baseline", settings=settings,
                  llm=make_llm(ScriptedCompletion([])), reference_store=UnconfiguredReferenceStore(),
                  state_store=SQLiteStateStore(settings.state_db_path))
    assert read_traces(settings.traces_path) == []


def test_baseline_prompt_states_exactly_the_config_policies():
    import config
    from agent.prompts import get_prompt
    p = get_prompt("baseline")
    block = "Policies:\n" + "\n".join(f"- {t}" for _, t in config.POLICIES)
    assert block in p  # rendered from the single source in config.py
    assert f"valid for {config.NON_EMPLOYMENT_VALIDITY_DAYS} days" in p
    assert p.index("Required documents:") < p.index("Policies:") < p.index("reply with ONLY this JSON")
    assert len({pid for pid, _ in config.POLICIES}) == len(config.POLICIES)  # unique ids for the guarded variant


def test_today_defaults_to_config_today(settings, make_llm, stores, pdf_with_hidden_text):
    import config
    completion = ScriptedCompletion([text_reply(json.dumps(FINAL))])
    r = _run(settings, make_llm, stores, completion, [pdf_with_hidden_text])
    assert r.trace.config["today"] == config.TODAY
    assert completion.calls[0]["messages"][1]["content"][0]["text"].startswith(f"Today's date: {config.TODAY}")


def test_qr_policy_uses_the_configured_registry_prefix():
    import config
    from agent.prompts import get_prompt
    assert dict(config.POLICIES)["qr_verified"].count(config.QR_VERIFY_PREFIX) == 1
    assert config.QR_VERIFY_PREFIX in get_prompt("baseline")
