"""TEST-ONLY fakes. Never import these from agent/ code and never treat them as data.

- ScriptedCompletion: stands in for litellm.completion, replaying a fixed script
  of model replies and recording what the model was sent.
- FakeReferenceStore: a tiny in-memory ReferenceStore with obviously synthetic
  records, only so tool calls have something to return.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from litellm import ModelResponse

from agent.datastore import UnknownRegistryError

FAKE_MODEL = "test/fake-model"


def tool_reply(*calls: tuple[str, dict[str, Any] | str], in_tok: int = 1000, out_tok: int = 50) -> ModelResponse:
    tool_calls = [
        {
            "id": f"call_{i}_{name}",
            "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)},
        }
        for i, (name, args) in enumerate(calls)
    ]
    return ModelResponse(
        model=FAKE_MODEL,
        choices=[{"index": 0, "finish_reason": "tool_calls",
                  "message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}],
        usage={"prompt_tokens": in_tok, "completion_tokens": out_tok, "total_tokens": in_tok + out_tok},
    )


def text_reply(text: str, in_tok: int = 1200, out_tok: int = 80) -> ModelResponse:
    return ModelResponse(
        model=FAKE_MODEL,
        choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        usage={"prompt_tokens": in_tok, "completion_tokens": out_tok, "total_tokens": in_tok + out_tok},
    )


class ScriptedCompletion:
    def __init__(self, replies: list[ModelResponse] | None = None, repeat_last: bool = False, raise_exc: Exception | None = None):
        self.replies = list(replies or [])
        self.repeat_last = repeat_last
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> ModelResponse:
        self.calls.append(copy.deepcopy(kwargs))
        if self.raise_exc:
            raise self.raise_exc
        i = len(self.calls) - 1
        if i < len(self.replies):
            return self.replies[i]
        if self.repeat_last and self.replies:
            return self.replies[-1]
        raise AssertionError("ScriptedCompletion ran out of replies")


class FakeReferenceStore:
    def __init__(self) -> None:
        self.data = {
            "fake_people": {"TEST-0001": {"name": "Test Person", "email": "registered@test.invalid"}},
        }
        self.transcripts = {"S-TEST-1": {"courses": [{"code": "TST101", "grade": "A"}]}}

    def registries(self) -> dict[str, str]:
        return {"fake_people": "synthetic test registry, key = test id"}

    def lookup(self, registry: str, key: str):
        if registry not in self.data:
            raise UnknownRegistryError(registry)
        return self.data[registry].get(key)

    def get_transcript(self, student_id: str):
        return self.transcripts.get(student_id)
