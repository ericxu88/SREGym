"""OpenAI-compatible adapter: message threading, tool calls, usage, degradation paths."""
from __future__ import annotations

import json
from typing import Any

from sregym.harness.agents.base import ToolCall
from sregym.harness.agents.openai_adapter import OpenAIAdapter
from sregym.tools.base import ToolResult


class _Resp:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    def __init__(self, responses: list[_Resp]):
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:  # noqa: A002
        self.requests.append({"url": url, "payload": json, "headers": headers})
        return self.responses.pop(0)


def _chat(content: str | None = None, tool_calls: list | None = None, finish: str = "stop",
          usage: dict | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20}}


def _adapter(responses: list[_Resp], **kw) -> tuple[OpenAIAdapter, _Client]:
    client = _Client(responses)
    a = OpenAIAdapter(model="test-model", client=client, **kw)
    a.start("SYSTEM", "PAGE", [{"name": "run_shell", "description": "d", "input_schema": {"type": "object"}}])
    return a, client


def test_tool_call_turn_and_observe():
    tc = {"id": "call_1", "type": "function",
          "function": {"name": "run_shell", "arguments": '{"command": "ls"}'}}
    a, client = _adapter([_Resp(200, _chat(content="looking", tool_calls=[tc], finish="tool_calls"))])
    turn = a.next_turn()
    assert turn.text == "looking" and not turn.stop
    assert turn.tool_calls[0].name == "run_shell" and turn.tool_calls[0].args == {"command": "ls"}
    assert turn.usage["input_tokens"] == 100 and turn.usage["output_tokens"] == 20
    a.observe([(turn.tool_calls[0], ToolResult("out"))])
    roles = [m["role"] for m in a.messages]
    assert roles == ["system", "user", "assistant", "tool"]
    assert a.messages[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "out"}
    # tools advertised in OpenAI function format
    payload = client.requests[0]["payload"]
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] == "run_shell"
    assert payload["max_tokens"] == 16000


def test_plain_reply_stops():
    a, _ = _adapter([_Resp(200, _chat(content="all done"))])
    turn = a.next_turn()
    assert turn.stop and turn.text == "all done" and not turn.tool_calls


def test_bad_tool_arguments_yield_empty_args():
    tc = {"id": "c", "type": "function", "function": {"name": "run_shell", "arguments": "{not json"}}
    a, _ = _adapter([_Resp(200, _chat(tool_calls=[tc], finish="tool_calls"))])
    assert a.next_turn().tool_calls[0].args == {}


def test_max_completion_tokens_fallback():
    a, client = _adapter([
        _Resp(400, text='{"error": {"message": "Unsupported parameter: max_tokens. Use max_completion_tokens"}}'),
        _Resp(200, _chat(content="ok")),
    ])
    assert a.next_turn().text == "ok"
    assert "max_completion_tokens" in client.requests[1]["payload"]
    assert "max_tokens" not in client.requests[1]["payload"]


def test_reasoning_effort_dropped_on_rejection():
    a, client = _adapter([
        _Resp(400, text='{"error": {"message": "unknown parameter reasoning_effort"}}'),
        _Resp(200, _chat(content="ok")),
    ], effort="high")
    assert a.next_turn().text == "ok"
    assert "reasoning_effort" in client.requests[0]["payload"]
    assert "reasoning_effort" not in client.requests[1]["payload"]


def test_retry_on_transient_status(monkeypatch):
    import sregym.harness.agents.openai_adapter as mod
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    a, client = _adapter([_Resp(429, text="rate limited"), _Resp(200, _chat(content="ok"))])
    assert a.next_turn().text == "ok"
    assert len(client.requests) == 2


def test_api_key_header(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-test")
    a, client = _adapter([_Resp(200, _chat(content="x"))], api_key_var="MY_KEY")
    a.next_turn()
    assert client.requests[0]["headers"]["Authorization"] == "Bearer sk-test"
