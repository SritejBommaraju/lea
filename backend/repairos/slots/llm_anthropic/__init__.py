"""LLM=anthropic (task B8): claude-sonnet-5-5 for plain-word text (report wording, B11).

anthropic SDK (Apache-2.0 since 2.0.0; MIT before), imported on first use. Key: ANTHROPIC_API_KEY via config.require (backend only, A6).
`cache_hint` becomes a second system block with `cache_control` (prompt caching on the product block). Structured
output uses `output_config.format` and is validated again with jsonschema; one retry with the error.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

import jsonschema

from repairos import spend
from repairos.config import get_settings
from repairos.registry import register
from repairos.slots.llm import Message, StructuredOutputError, Tool, Transcript

DEFAULT_MODEL = "claude-sonnet-5-5"


@register("LLM", "anthropic")
class AnthropicTextModel:
    def __init__(self, *, model: str = DEFAULT_MODEL, effort: str = "medium", max_tokens: int = 16000):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = None
        self._lock = threading.Lock()

    @property
    def client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    import anthropic

                    key = get_settings().require("ANTHROPIC_API_KEY", "LLM=anthropic")
                    self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def _system(self, system: str, cache_hint: str | None) -> list[dict]:
        blocks = [{"type": "text", "text": system}]
        if cache_hint:
            blocks.append({"type": "text", "text": cache_hint, "cache_control": {"type": "ephemeral"}})
        return blocks

    def _create(self, system: str, messages: list[Message], cache_hint: str | None, **extra):
        output_config = {"effort": self.effort, **extra.pop("output_config", {})}
        sys_blocks = self._system(system, cache_hint)
        # Spend guard first (repairos.spend): refused before anything is spent; key = the whole request.
        est = spend.estimate_usd("anthropic")
        spend.charge("anthropic", usd=est, key=hashlib.sha256(json.dumps(
            [self.model, sys_blocks, messages], sort_keys=True, default=str).encode()).hexdigest()[:32])
        resp = self.client.messages.create(model=self.model, max_tokens=self.max_tokens, system=sys_blocks,
                                           messages=messages, output_config=output_config, **extra)
        u = getattr(resp, "usage", None)
        usd = spend.anthropic_usd(self.model, {k: getattr(u, k, 0) or 0 for k in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}) if u else None
        if usd is not None:
            spend.record_cost("anthropic", usd, estimate=est)
        return resp

    @staticmethod
    def _text(resp) -> str:
        if resp.stop_reason == "refusal":
            raise RuntimeError("the model declined (stop_reason=refusal)")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("the model hit max_tokens before finishing")
        return "".join(b.text for b in resp.content if b.type == "text")

    def complete(self, system: str, messages: list[Message], *, cache_hint: str | None = None) -> str:
        return self._text(self._create(system, messages, cache_hint))

    def structured(self, schema: dict[str, Any], system: str, messages: list[Message], *,
                   cache_hint: str | None = None) -> dict[str, Any]:
        msgs = list(messages)
        err = None
        for _ in range(2):
            resp = self._create(system, msgs, cache_hint,
                                output_config={"format": {"type": "json_schema", "schema": schema}})
            text = self._text(resp)
            try:
                data = json.loads(text)
                jsonschema.validate(data, schema)
                return data
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                err = e
                msgs = msgs + [{"role": "assistant", "content": text},
                               {"role": "user", "content": f"That answer is invalid: {str(e)[:500]}. Answer again, "
                                                           "valid against the schema."}]
        raise StructuredOutputError(f"no schema-valid answer after a retry: {err}")

    def tool_loop(self, system: str, messages: list[Message], tools: list[Tool], *, max_turns: int = 8,
                  cache_hint: str | None = None) -> Transcript:
        by_name = {t.name: t for t in tools}
        api_tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
        tr = Transcript(messages=list(messages), model=self.model)
        while True:
            if tr.turns >= max_turns:
                tr.stopped = "max_turns"
                return tr
            resp = self._create(system, tr.messages, cache_hint, tools=api_tools)
            tr.turns += 1
            tr.messages.append({"role": "assistant", "content": resp.content})
            tr.final_text = "".join(b.text for b in resp.content if b.type == "text")
            if resp.stop_reason != "tool_use":
                tr.stopped = resp.stop_reason
                return tr
            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                call = {"name": b.name, "input": b.input}
                tool = by_name.get(b.name)
                try:
                    if tool is None:
                        raise KeyError(f"unknown tool {b.name}")
                    out = tool.fn(b.input)
                    call["output"] = out
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": out if isinstance(out, str) else json.dumps(out)})
                except Exception as e:  # the model sees the error and may recover; the transcript keeps it
                    call["error"] = f"{type(e).__name__}: {e}"
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": call["error"],
                                    "is_error": True})
                tr.tool_calls.append(call)
            tr.messages.append({"role": "user", "content": results})
