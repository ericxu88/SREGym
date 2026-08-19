"""Adapter plumbing against a fake Anthropic client (no network)."""
from __future__ import annotations

from types import SimpleNamespace as NS

import httpx
import pytest

anthropic = pytest.importorskip("anthropic")

from sregym.harness.agents.anthropic_adapter import AnthropicAdapter  # noqa: E402
from sregym.harness.agents.base import ToolCall  # noqa: E402
from sregym.tools.base import ToolResult  # noqa: E402


def _msg(content, stop_reason="tool_use", usage=None):
    usage = usage or {}
    return NS(content=content, stop_reason=stop_reason, stop_details=None,
              usage=NS(input_tokens=usage.get("in", 100), output_tokens=usage.get("out", 20),
                       cache_read_input_tokens=usage.get("cr", 0), cache_creation_input_tokens=usage.get("cw", 0)))


class FakeMessages:
    def __init__(self, responses, fail_first_with=None):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.fail_first_with = fail_first_with

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first_with is not None:
            exc, self.fail_first_with = self.fail_first_with, None
            raise exc
        return self.responses.pop(0)


def _client(responses, **kw):
    return NS(messages=FakeMessages(responses, **kw))


def test_tool_use_round_trip_and_usage():
    responses = [
        _msg([NS(type="thinking", thinking="look at logs first"), NS(type="text", text="Checking logs."),
              NS(type="tool_use", id="tu_1", name="read_logs", input={"path": "x.log"})], usage={"in": 1000, "out": 50, "cw": 900}),
        _msg([NS(type="text", text="Done.")], stop_reason="end_turn", usage={"in": 10, "cr": 1900}),
    ]
    a = AnthropicAdapter(model="claude-opus-5", client=_client(responses))
    a.start("SYS", "TASK", [{"name": "read_logs", "description": "d", "input_schema": {"type": "object", "properties": {}}}])
    turn = a.next_turn()
    assert turn.tool_calls == [ToolCall("tu_1", "read_logs", {"path": "x.log"})]
    assert turn.text == "Checking logs." and turn.thinking == "look at logs first" and not turn.stop
    assert turn.usage["input_tokens"] == 1900 and turn.usage["output_tokens"] == 50
    a.observe([(turn.tool_calls[0], ToolResult("LOG LINES", is_error=False))])
    turn2 = a.next_turn()
    assert turn2.stop and turn2.text == "Done." and turn2.usage["cache_read_input_tokens"] == 1900
    calls = a.client.messages.calls
    req = calls[1]
    assert req["model"] == "claude-opus-5" and req["thinking"]["type"] == "adaptive"
    assert req["system"][0]["text"] == "SYS" and req["cache_control"] == {"type": "ephemeral"}
    assert req["tools"][0]["name"] == "read_logs"
    msgs = req["messages"]
    assert msgs[0] == {"role": "user", "content": "TASK"}
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"] is responses[0].content  # thinking blocks echoed back unchanged
    assert msgs[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "LOG LINES", "is_error": False}]}
    # Opus 5 -> server-side fallbacks requested by default
    assert req["extra_body"] == {"fallbacks": "default"} and "server-side-fallback" in req["extra_headers"]["anthropic-beta"]


def test_fallback_beta_rejected_then_retried_without():
    err = anthropic.BadRequestError("fallbacks not available for this beta", response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)
    responses = [_msg([NS(type="text", text="ok")], stop_reason="end_turn")]
    a = AnthropicAdapter(model="claude-fable-5", client=_client(responses, fail_first_with=err))
    a.start("SYS", "TASK", [])
    turn = a.next_turn()
    assert turn.stop and a.fallbacks is False
    assert len(a.client.messages.calls) == 2 and "extra_body" not in a.client.messages.calls[1]


def test_refusal_stops_the_episode():
    resp = _msg([NS(type="text", text="")], stop_reason="refusal")
    resp.stop_details = NS(category="cyber", explanation="nope")
    a = AnthropicAdapter(model="claude-sonnet-5", client=_client([resp]))
    a.start("SYS", "TASK", [])
    turn = a.next_turn()
    assert turn.stop and "refused" in turn.text and "cyber" in turn.text
    assert "extra_body" not in a.client.messages.calls[0]  # no fallbacks on non-Fable/Opus-5 by default


def test_multiple_tool_calls_return_in_one_user_message():
    resp = _msg([NS(type="tool_use", id="a", name="read_logs", input={}), NS(type="tool_use", id="b", name="query_metrics", input={})])
    a = AnthropicAdapter(client=_client([resp, _msg([NS(type="text", text="x")], stop_reason="end_turn")]))
    a.start("S", "T", [])
    turn = a.next_turn()
    assert [c.id for c in turn.tool_calls] == ["a", "b"]
    a.observe([(turn.tool_calls[0], ToolResult("r1")), (turn.tool_calls[1], ToolResult("boom", is_error=True))])
    a.nudge("continue")
    a.next_turn()
    msgs = a.client.messages.calls[1]["messages"]
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["a", "b"] and msgs[2]["content"][1]["is_error"] is True
    assert msgs[3] == {"role": "user", "content": "continue"}


def test_run_episode_with_fake_model(tmp_path):
    """The harness drives the Anthropic adapter end to end (fake model: one tool call, then resolve)."""
    from sregym.harness.episode import EpisodeConfig, run_episode
    from sregym.harness.trajectory import read_trajectory
    from tests.conftest import HISTORY_MINUTES

    responses = [
        _msg([NS(type="thinking", thinking="start with the page"), NS(type="text", text="Looking at recent errors."),
              NS(type="tool_use", id="t1", name="read_logs", input={"path": "checkout-service/logs/app.log", "tail": True, "limit": 3})]),
        _msg([NS(type="tool_use", id="t2", name="restart_service", input={})]),
        _msg([NS(type="tool_use", id="t3", name="resolve_incident", input={"summary": "restarted", "root_cause": "?"})]),
    ]
    agent = AnthropicAdapter(model="claude-opus-5", client=_client(responses))
    res = run_episode(agent, EpisodeConfig(seed=9, out_dir=tmp_path / "out", history_minutes=HISTORY_MINUTES,
                                           workdir=tmp_path / "w", live_traffic=False))
    assert res.stop_reason == "resolved" and res.steps == 3 and res.reward == 0.0  # masked, not fixed
    meta, steps, end = read_trajectory(tmp_path / "out" / "trajectory.jsonl")
    assert meta["agent"]["model"] == "claude-opus-5"
    assert steps[0]["assistant_thinking"] == "start with the page" and steps[0]["usage"]["input_tokens"] == 100
    assert "app.log" in steps[0]["tool_result"] and steps[1]["tool_call"] == "restart_service"
    # the model saw the task prompt first and every tool result went back as tool_result blocks
    first_call = agent.client.messages.calls[0]
    assert first_call["messages"][0]["content"].startswith("[PagerDuty]")
    msgs = agent.client.messages.calls[2]["messages"]  # live list: [..., tool_result(t2), assistant(turn 3)]
    assert msgs[-2]["content"][0]["tool_use_id"] == "t2" and "checkout-service" in msgs[-2]["content"][0]["content"]
