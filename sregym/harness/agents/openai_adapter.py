"""OpenAI-compatible Chat Completions adapter (tool calling, manual loop).

One adapter covers every OpenAI-compatible endpoint: OpenAI itself, vLLM, Ollama,
OpenRouter, Together, and Anthropic's compatibility endpoint — pick with ``base_url``
(or ``$OPENAI_BASE_URL``) and ``api_key_var``. No provider SDK: a thin httpx client
with bounded timeouts and 429/5xx retries.

The harness owns the loop (identical logging/verification for every provider); this
adapter only turns "next turn" into one ``/chat/completions`` call and feeds tool
results back as ``role: tool`` messages.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from sregym.harness.agents.base import AgentAdapter, AgentTurn, ToolCall
from sregym.tools.base import ToolResult

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class OpenAIAdapter(AgentAdapter):
    name = "openai"

    def __init__(self, model: str = "gpt-5.2", max_tokens: int = 16000, base_url: str | None = None,
                 api_key_var: str = "OPENAI_API_KEY", effort: str | None = None,
                 max_retries: int = 4, request_timeout: float = 240.0, client: Any = None, **_ignored: Any):
        import httpx

        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key_var = api_key_var
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort  # forwarded as reasoning_effort when set; dropped if the server rejects it
        self.max_retries = max_retries
        self._client = client or httpx.Client(timeout=httpx.Timeout(request_timeout, connect=10.0))
        self._max_tokens_key = "max_tokens"  # swapped to max_completion_tokens if the server demands it
        self._send_effort = effort is not None
        self.messages: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.last_response: Any = None

    def describe(self) -> dict[str, Any]:
        return {"agent": self.name, "model": self.model, "base_url": self.base_url,
                "max_tokens": self.max_tokens, "effort": self.effort}

    # ------------------------------------------------------------------ AgentAdapter API
    def start(self, system_prompt: str, task_prompt: str, tool_specs: list[dict[str, Any]]) -> None:
        self.tools = [{"type": "function",
                       "function": {"name": t["name"], "description": t["description"],
                                    "parameters": t["input_schema"]}} for t in tool_specs]
        self.messages = [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": task_prompt}]

    def next_turn(self) -> AgentTurn:
        data = self._create()
        self.last_response = data
        choice = data["choices"][0]
        message = choice["message"]
        self.messages.append(self._echo(message))
        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        turn_usage = {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "cache_read_input_tokens": int(details.get("cached_tokens") or 0),
            "cache_creation_input_tokens": 0,
        }
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}
            calls.append(ToolCall(id=tc.get("id") or f"call-{uuid.uuid4().hex[:8]}",
                                  name=fn.get("name") or "", args=args))
        text = (message.get("content") or "").strip() or None
        reasoning = (message.get("reasoning_content") or message.get("reasoning") or "") or None
        stop = not calls and choice.get("finish_reason") in ("stop", "length", "content_filter", None)
        return AgentTurn(text=text, tool_calls=calls, usage=turn_usage, stop=stop,
                         thinking=reasoning if isinstance(reasoning, str) else None)

    @staticmethod
    def _echo(message: dict[str, Any]) -> dict[str, Any]:
        """Echo the assistant message back verbatim minus provider extras some servers reject."""
        out: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            out["tool_calls"] = message["tool_calls"]
        return out

    def observe(self, results: list[tuple[ToolCall, ToolResult]]) -> None:
        for call, result in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id,
                                  "content": result.content or "(no output)"})

    def nudge(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    # ------------------------------------------------------------------ request
    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": self.messages,
                                   self._max_tokens_key: self.max_tokens}
        if self.tools:
            payload["tools"] = self.tools
        if self._send_effort:
            payload["reasoning_effort"] = self.effort
        return payload

    def _create(self) -> dict[str, Any]:
        import httpx

        key = os.environ.get(self.api_key_var, "")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = f"{self.base_url}/chat/completions"
        attempt = 0
        while True:
            try:
                resp = self._client.post(url, json=self._payload(), headers=headers)
            except httpx.HTTPError as e:
                if attempt >= self.max_retries:
                    raise RuntimeError(f"openai request failed after {attempt + 1} attempts: {e}") from e
                attempt += 1
                time.sleep(min(30.0, 1.5 * 2 ** attempt))
                continue
            if resp.status_code == 400:
                body = resp.text.lower()
                if self._max_tokens_key == "max_tokens" and "max_completion_tokens" in body:
                    self._max_tokens_key = "max_completion_tokens"
                    continue
                if self._send_effort and ("reasoning_effort" in body or "reasoning" in body):
                    self._send_effort = False
                    continue
            if resp.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                attempt += 1
                time.sleep(min(30.0, 1.5 * 2 ** attempt))
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"openai request failed: {resp.status_code} {resp.text[:300]}")
            return resp.json()
