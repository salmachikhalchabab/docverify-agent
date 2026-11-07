"""The hand-written agent loop: Perceive -> Think -> Act -> Observe -> repeat.

No agent framework: every model call and every tool call is one Step in the trace.

Termination:
- the model replies without tool calls -> that reply is the final answer;
  it is parsed with json.loads; the only normalization is unwrapping a reply
  that is one Markdown code block (```json ... ```); otherwise no repair;
  failure -> parse_error=True.
- MAX_STEPS model calls reached while the model still wants tools ->
  hit_max_steps=True (a lab safety net, not a guardrail).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from agent.documents import Document
from agent.llm import LLMClient
from agent.tools import ToolContext, execute_tool
from agent.trace import RunTrace, Step
from agent.variants import Guardrails


def build_user_content(
    service: str, requester_email: str, documents: list[Document], today: str, request_text: str | None = None
) -> list[dict[str, Any]]:
    """First user message: date + request + every page as image + its full extracted text.

    `today` (YYYY-MM-DD) is given so validity periods can be checked; it is the
    same for every variant. `request_text` is the requester's free-text message as
    typed (untrusted, like the documents); omitted when there is none.

    Content is passed through unmarked: no delimiters, no "untrusted" labels
    (that would be a mitigation, which the baseline doesn't have).
    """
    parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Today's date: {today}\n"
                f"Requested service: {service}\n"
                f"Requester email: {requester_email}\n"
                + (f"Requester's message: {request_text}\n" if request_text else "")
                + f"Number of uploaded documents: {len(documents)}"
            ),
        }
    ]
    for d_i, doc in enumerate(documents, 1):
        for page in doc.pages:
            parts.append(
                # File names are not shown: they are upload metadata, and in the evaluation
                # they would leak the case label. The trace records the full paths.
                {"type": "text", "text": f"\nDocument {d_i} (page {page.page_number} of {doc.total_pages})"}
            )
            parts.append({"type": "image_url", "image_url": {"url": page.image_data_url}})
            label = {"pdf_text_layer": "Extracted text", "ocr": "Extracted text (OCR)"}.get(page.text_source)
            parts.append(
                {"type": "text", "text": f"{label}:\n{page.text}" if label else "Extracted text: (none)"}
            )
    return parts


# A reply that is exactly one Markdown code block: ```json ... ``` or ``` ... ```
_FENCED = re.compile(r"^```[a-zA-Z]*[ \t]*\n(.*?)\n?```$", re.DOTALL)


def _parse_final(text: str | None) -> tuple[Any, bool, bool]:
    """Returns (parsed, parse_error, fenced).

    The only normalization: if the whole reply is one Markdown code block, its
    content is parsed (decided with the user: a formatting habit, not a decision
    error; applied identically to every variant). Anything else that is not
    valid JSON stays a parse error. No other repair.
    """
    if text is None:
        return None, True, False
    s = text.strip()
    m = _FENCED.match(s)
    fenced = m is not None
    if fenced:
        s = m.group(1).strip()
    try:
        return json.loads(s), False, fenced
    except (json.JSONDecodeError, ValueError):
        return None, True, fenced


def run_loop(
    *,
    trace: RunTrace,
    llm: LLMClient,
    system_prompt: str,
    user_content: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    tool_functions: dict[str, Callable[..., Any]],
    ctx: ToolContext,
    guardrails: Guardrails,
    max_steps: int,
    on_step: Callable[[Step], None] | None = None,
) -> None:
    """Runs to completion and fills `trace` in place. Exceptions from the model call propagate."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    step_no = 0

    def _record(step: Step) -> None:
        trace.steps.append(step)
        if on_step is not None:  # observation only (e.g. a live UI); never changes behavior
            try:
                on_step(step)
            except Exception:
                pass

    for iteration in range(1, max_steps + 1):
        resp = llm.complete(messages, tool_schemas)
        step_no += 1
        _record(
            Step(
                step=step_no,
                type="model_call",
                iteration=iteration,
                latency_ms=resp.latency_ms,
                text=resp.text,
                requested_tool_calls=[
                    {"id": tc.id, "name": tc.name, "arguments_raw": tc.arguments_raw} for tc in resp.tool_calls
                ]
                or None,
                finish_reason=resp.finish_reason,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cached_input_tokens=resp.cached_input_tokens,
                reasoning_tokens=resp.reasoning_tokens,
                reasoning_text=resp.reasoning_text,
                cost_usd=resp.cost_usd,
                cost_source=resp.cost_source,
                response_model=resp.response_model,
                attempts=resp.attempts,
                retry_errors=resp.retry_errors or None,
                retry_overhead_ms=resp.retry_overhead_ms,
            )
        )
        messages.append(resp.assistant_message)

        if not resp.tool_calls:
            parsed, parse_error, fenced = _parse_final(resp.text)
            trace.final_text_raw = resp.text
            trace.final_output = parsed
            trace.parse_error = parse_error
            trace.final_fenced = fenced
            decision = parsed.get("decision") if isinstance(parsed, dict) else None
            trace.decision = guardrails.finalize(decision, parsed, ctx)
            # "truncated": the reply was cut off by the max_tokens limit (a setting, not a model choice)
            trace.status = "truncated" if resp.finish_reason == "length" else "completed"
            return

        for tc in resp.tool_calls:
            t_hook = time.perf_counter()
            guardrails.current_step = step_no + 1  # the step this tool call will be recorded as
            override = guardrails.intercept_tool_call(tc.name, tc.arguments_raw, ctx)
            if override is not None:
                args: Any = None
                try:
                    args = json.loads(tc.arguments_raw)
                except (json.JSONDecodeError, ValueError):
                    pass
                latency = (time.perf_counter() - t_hook) * 1000.0
                result, error = override, override.get("error")
            else:
                out = execute_tool(ctx, tc.name, tc.arguments_raw, tool_functions)
                args, result, error, latency = out.arguments, out.result, out.error, out.latency_ms
            step_no += 1
            _record(
                Step(
                    step=step_no,
                    type="tool_call",
                    iteration=iteration,
                    latency_ms=latency,
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    tool_arguments=args,
                    tool_arguments_raw=tc.arguments_raw,
                    tool_result=result,
                    tool_error=error,
                )
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

    trace.hit_max_steps = True
    trace.status = "hit_max_steps"
    trace.decision = guardrails.finalize(None, None, ctx)
