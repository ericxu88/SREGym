"""Anthropic Messages API adapter (tool use, manual loop driven by the harness).

Why a manual loop rather than the SDK tool runner: the harness owns the agent loop so
that every provider adapter is interchangeable and every tool call is logged/verified
uniformly. The adapter only turns "next turn" into one ``messages.create`` call and
feeds tool results back.

Defaults: ``claude-opus-5``, adaptive thinking (thinking blocks are echoed back
unchanged on the next turn), automatic prompt caching (the growing transcript is a
stable prefix), and server-side refusal fallbacks for Fable 5 / Opus 5.
"""
from __future__ import annotations

import os
from typing import Any

from sregym.harness.agents.base import AgentAdapter, AgentTurn, ToolCall
from sregym.tools.base import ToolResult

DEFAULT_MODEL = "claude-opus-5"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicAdapter(AgentAdapter):
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 16000, thinking: str = "adaptive",
                 effort: str | None = None, thinking_display: str = "summarized", fallbacks: bool | None = None,
                 max_retries: int = 4, request_timeout: float = 240.0, client: Any = None):
        import anthropic

        self._anthropic = anthropic
        # explicit, bounded timeouts: on flaky networks a dead connection can otherwise hang a read for
        # hours (observed: SSL read blocked ~4h during a sweep). connect fast-fails; the SDK then retries.
        # The Timeout class must come from the HTTP library the SDK itself uses: anthropic 1.x moved
        # from httpx to httpx2, and a classic httpx.Timeout breaks its transport at request time.
        self.client = client or anthropic.Anthropic(
            max_retries=max_retries, timeout=_sdk_timeout(anthropic, request_timeout))
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking  # "adaptive" | "off"
        self.effort = effort
        self.thinking_display = thinking_display
        # server-side refusal fallbacks are recommended for Fable 5 / Opus 5; auto-disable if the API rejects them
        self.fallbacks = fallbacks if fallbacks is not None else model.startswith(("claude-fable-5", "claude-opus-5"))
        self.system: str = ""
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.last_response: Any = None

    def describe(self) -> dict[str, Any]:
        return {"agent": self.name, "model": self.model, "thinking": self.thinking, "effort": self.effort,
                "max_tokens": self.max_tokens, "fallbacks": self.fallbacks}

    # ------------------------------------------------------------------ AgentAdapter API
    def start(self, system_prompt: str, task_prompt: str, tool_specs: list[dict[str, Any]]) -> None:
        self.system = system_prompt
        self.tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tool_specs]
        self.messages = [{"role": "user", "content": task_prompt}]

    def next_turn(self) -> AgentTurn:
        response = self._create()
        self.last_response = response
        # echo the full content (thinking + text + tool_use blocks) back on the next request
        self.messages.append({"role": "assistant", "content": response.content})
        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        turn_usage = {
            "input_tokens": int(usage.input_tokens + cache_read + cache_write),
            "output_tokens": int(usage.output_tokens),
            "cache_read_input_tokens": int(cache_read),
            "cache_creation_input_tokens": int(cache_write),
        }
        text_parts, thinking_parts, calls = [], [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking" and getattr(block, "thinking", ""):
                thinking_parts.append(block.thinking)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input or {})))
        text = "\n".join(p for p in text_parts if p).strip() or None
        thinking = "\n".join(thinking_parts).strip() or None
        stop = response.stop_reason in ("end_turn", "refusal", "stop_sequence") and not calls
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            text = (text or "") + f"\n[model refused: {getattr(details, 'category', None)} {getattr(details, 'explanation', '')}]"
            stop = True
        return AgentTurn(text=text, tool_calls=calls, usage=turn_usage, stop=stop, thinking=thinking)

    def observe(self, results: list[tuple[ToolCall, ToolResult]]) -> None:
        # all tool results for a turn go back in ONE user message
        blocks = [{"type": "tool_result", "tool_use_id": call.id, "content": result.content or "(no output)",
                   "is_error": bool(result.is_error)} for call, result in results]
        # tool_use blocks that were not executed (episode ended mid-turn) never get here; fine.
        self.messages.append({"role": "user", "content": blocks})

    def nudge(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    # ------------------------------------------------------------------ request
    def _request_kwargs(self, with_fallbacks: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            "tools": self.tools,
            "messages": self.messages,
            "cache_control": {"type": "ephemeral"},  # auto-cache the growing transcript prefix
        }
        if self.thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive", "display": self.thinking_display}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if with_fallbacks:
            kwargs["extra_headers"] = {"anthropic-beta": _FALLBACK_BETA}
            kwargs["extra_body"] = {"fallbacks": "default"}
        return kwargs

    def _create(self) -> Any:
        anthropic = self._anthropic
        try:
            return self.client.messages.create(**self._request_kwargs(self.fallbacks))
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            if self.fallbacks and ("fallback" in msg or "beta" in msg):
                # account/platform without server-side fallbacks: retry once without them, then remember
                self.fallbacks = False
                return self.client.messages.create(**self._request_kwargs(False))
            if self.thinking == "adaptive" and "thinking" in msg:
                # older models (Sonnet 4.5 / Haiku 4.5) do not accept adaptive thinking
                self.thinking = "off"
                return self.client.messages.create(**self._request_kwargs(self.fallbacks))
            raise


def _sdk_timeout(anthropic, request_timeout: float):  # noqa: ANN001, ANN202
    """A read/write-bounded, fast-connect timeout in the SDK's own HTTP library's terms."""
    base = getattr(anthropic, "_base_client", None)
    http_mod = getattr(base, "httpx2", None) or getattr(base, "httpx", None)
    if http_mod is None:  # unknown SDK layout: a plain float is universally accepted
        return request_timeout
    return http_mod.Timeout(request_timeout, connect=10.0)


def api_credentials_present() -> bool:
    """Best-effort check used only for a friendlier CLI message (the SDK also reads `ant auth login` profiles)."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return os.path.isdir(os.path.expanduser("~/.config/anthropic"))
