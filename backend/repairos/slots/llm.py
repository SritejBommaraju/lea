"""LLM slot interface (task B8). Implementations live in repairos.slots.llm_<impl>.

- `complete(system, messages, *, cache_hint=None) -> str`
- `structured(schema, system, messages, *, cache_hint=None) -> dict`: validated against `schema` (jsonschema);
  one retry with the validation error, then `StructuredOutputError`.
- `tool_loop(system, messages, tools, *, max_turns) -> Transcript`: calls tools until the model stops or max_turns.

`cache_hint` is a long, stable block (the product block in Phase 2) the implementation may cache. Data from OCR, users
or the web goes into `messages` as quoted data, never into `system` (A9). Numbers are compared in code; the model
gets words (S5).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

Message = dict[str, Any]  # {"role": "user" | "assistant", "content": str | list[block]}


class StructuredOutputError(ValueError):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[[dict[str, Any]], Any]  # returns something JSON-serialisable or a str


@dataclass
class Transcript:
    messages: list[Message] = field(default_factory=list)
    final_text: str = ""
    turns: int = 0
    stopped: str = ""          # end_turn | max_turns | refusal | max_tokens
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {name, input, output | error}
    model: str = ""


class TextModel(Protocol):
    model: str

    def complete(self, system: str, messages: list[Message], *, cache_hint: str | None = None) -> str: ...

    def structured(self, schema: dict[str, Any], system: str, messages: list[Message], *,
                   cache_hint: str | None = None) -> dict[str, Any]: ...

    def tool_loop(self, system: str, messages: list[Message], tools: list[Tool], *, max_turns: int = 8,
                  cache_hint: str | None = None) -> Transcript: ...
