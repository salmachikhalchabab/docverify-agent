"""LiteLLM wrapper: one call in, one normalized response out (message + usage + cost + latency).

The model is whatever LLM_MODEL says; nothing here is provider-specific.
`completion_fn` is injectable so tests can replace the network call; the
production default is litellm.completion.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import litellm


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_raw: str  # exactly what the model produced (a JSON string, usually)


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    finish_reason: str | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int | None
    reasoning_tokens: int | None  # part of output_tokens, if the provider reports it
    reasoning_text: str | None  # the model's visible reasoning, if the provider returns it
    cost_usd: float | None
    cost_source: str  # "litellm" | "price_table" | "unknown"
    latency_ms: float
    response_model: str | None  # model name the provider reported
    attempts: int = 1  # 1 = succeeded first time
    retry_errors: list[str] = field(default_factory=list)  # one entry per failed attempt
    retry_overhead_ms: float = 0.0  # time spent on failed attempts + backoff waits
    assistant_message: dict[str, Any] = field(default_factory=dict)  # to append to history


# Provider-side failures worth retrying (server errors, overload, dropped connections, timeouts).
# Rate limits are handled separately: per-minute quotas wait and retry, daily quotas do not.
TRANSIENT_ERRORS = (
    litellm.InternalServerError,
    litellm.ServiceUnavailableError,
    litellm.APIConnectionError,
    litellm.Timeout,
)


def per_minute_rate_limit_wait(e: Exception) -> float | None:
    """Seconds to wait if `e` is a PER-MINUTE quota error (429); None otherwise.

    Daily quotas are not retried. LiteLLM maps some Gemini 429s to BadRequestError,
    so the check looks at the message, not only the exception type.
    """
    msg = str(e)
    if "429" not in msg and not isinstance(e, litellm.RateLimitError):
        return None
    low = msg.lower()
    if "per day" in low or "perday" in low:
        return None
    if "per minute" not in low and "perminute" not in low:
        return None
    m = re.search(r'retryDelay"?\s*:\s*"?(\d+(?:\.\d+)?)s', msg) or re.search(r"retry in (\d+(?:\.\d+)?)s", low)
    return min(float(m.group(1)) + 2.0, 90.0) if m else 62.0


def model_capabilities(model: str) -> dict[str, bool | None]:
    """What LiteLLM knows about vision / tool support. None = LiteLLM doesn't know the model.

    (litellm.supports_* return False for unknown models, which would be misleading.)
    """
    try:
        litellm.get_model_info(model=model)
    except Exception:
        return {"vision": None, "function_calling": None}
    caps: dict[str, bool | None] = {}
    for key, fn in (("vision", litellm.supports_vision), ("function_calling", litellm.supports_function_calling)):
        try:
            caps[key] = bool(fn(model=model))
        except Exception:
            caps[key] = None
    return caps


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class LLMClient:
    def __init__(
        self,
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_s: int | None = None,
        num_retries: int = 0,
        price_table: dict[str, dict[str, float]] | None = None,
        completion_fn: Callable[..., Any] | None = None,
        backoff_s: float = 2.0,
        rate_limit_retries: int = 4,
        rate_limit_wait_scale: float = 1.0,  # tests set this to ~0
    ):
        if not model:
            raise ValueError("LLM_MODEL is not set. Put a LiteLLM model string in .env.")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.num_retries = num_retries
        self.price_table = price_table or {}
        self._completion = completion_fn or litellm.completion
        self.backoff_s = backoff_s
        self.rate_limit_retries = rate_limit_retries
        self.rate_limit_wait_scale = rate_limit_wait_scale

    def call_params(self) -> dict[str, Any]:
        """The sampling parameters actually sent (recorded in the trace)."""
        p: dict[str, Any] = {}
        if self.temperature is not None:
            p["temperature"] = self.temperature
        if self.max_tokens is not None:
            p["max_tokens"] = self.max_tokens
        return p

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> LLMResponse:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, **self.call_params()}
        if tools:
            kwargs["tools"] = tools
        if self.timeout_s:
            kwargs["timeout"] = self.timeout_s
        # Retries are done here (not inside LiteLLM) so each one is visible in the trace.
        kwargs["num_retries"] = 0
        retry_errors: list[str] = []
        t_first = time.perf_counter()
        while True:
            t0 = time.perf_counter()
            try:
                resp = self._completion(**kwargs)
                latency_ms = (time.perf_counter() - t0) * 1000.0
                break
            except Exception as e:
                wait = per_minute_rate_limit_wait(e)
                if wait is not None:  # per-minute quota: wait for the window to reset
                    if sum(r.startswith("rate_limit") for r in retry_errors) >= self.rate_limit_retries:
                        raise
                    retry_errors.append(f"rate_limit_per_minute (waited {wait:.0f}s): {type(e).__name__}")
                    time.sleep(wait * self.rate_limit_wait_scale)
                    continue
                if not isinstance(e, TRANSIENT_ERRORS):
                    raise
                if sum(not r.startswith("rate_limit") for r in retry_errors) >= self.num_retries:
                    raise
                retry_errors.append(f"{type(e).__name__}: {str(e)[:200]}")
                time.sleep(self.backoff_s * (2 ** (len(retry_errors) - 1)) * (1 + random.random() * 0.25))
        retry_overhead_ms = (time.perf_counter() - t_first) * 1000.0 - latency_ms

        choice = _get(resp, "choices")[0]
        msg = _get(choice, "message")
        text = _get(msg, "content")
        tool_calls = [
            ToolCall(
                id=_get(tc, "id"),
                name=_get(_get(tc, "function"), "name"),
                arguments_raw=_get(_get(tc, "function"), "arguments") or "",
            )
            for tc in (_get(msg, "tool_calls") or [])
        ]

        usage = _get(resp, "usage")
        in_tok = int(_get(usage, "prompt_tokens", 0) or 0)
        out_tok = int(_get(usage, "completion_tokens", 0) or 0)
        details = _get(usage, "prompt_tokens_details")
        cached = _get(details, "cached_tokens")
        reasoning = _get(_get(usage, "completion_tokens_details"), "reasoning_tokens")

        cost, source = self._cost(resp, in_tok, out_tok)

        assistant_message: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            assistant_message["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments_raw}}
                for tc in tool_calls
            ]

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=_get(choice, "finish_reason"),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_input_tokens=int(cached) if cached is not None else None,
            reasoning_tokens=int(reasoning) if reasoning is not None else None,
            reasoning_text=_get(msg, "reasoning_content") or None,
            cost_usd=cost,
            cost_source=source,
            latency_ms=latency_ms,
            response_model=_get(resp, "model"),
            attempts=len(retry_errors) + 1,
            retry_errors=retry_errors,
            retry_overhead_ms=retry_overhead_ms,
            assistant_message=assistant_message,
        )

    def _cost(self, resp: Any, in_tok: int, out_tok: int) -> tuple[float | None, str]:
        try:
            c = litellm.completion_cost(completion_response=resp, model=self.model)
            if c is not None:
                return float(c), "litellm"
        except Exception:
            pass
        price = self.price_table.get(self.model)
        if price:
            c = in_tok / 1e6 * price["input_per_mtok"] + out_tok / 1e6 * price["output_per_mtok"]
            return c, "price_table"
        return None, "unknown"
