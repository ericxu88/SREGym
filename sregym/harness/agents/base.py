"""Model-agnostic agent interface.

The harness drives the loop; an adapter only has to (1) accept the prompts and tool
specs, (2) produce the next turn (text and/or tool calls with usage), and (3) receive
tool results. Tool specs are Anthropic-style ``{name, description, input_schema}``
dicts -- an OpenAI adapter would wrap each as
``{"type": "function", "function": {"name", "description", "parameters": input_schema}}``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sregym.tools.base import ToolResult


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class AgentTurn:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)  # input_tokens, output_tokens (per turn)
    stop: bool = False  # the agent explicitly ended without a tool call
    thinking: str | None = None  # optional reasoning summary (recorded in the trajectory, never shown to tools)


class AgentAdapter:
    name: str = "base"

    def bind_world(self, world) -> None:  # noqa: ANN001 - harness hook
        """Called by the harness before start() with the generated world. LLM adapters ignore it;
        scripted/test agents may read the world's stack naming from it."""

    def start(self, system_prompt: str, task_prompt: str, tool_specs: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def next_turn(self) -> AgentTurn:
        raise NotImplementedError

    def observe(self, results: list[tuple[ToolCall, ToolResult]]) -> None:
        raise NotImplementedError

    def nudge(self, text: str) -> None:
        """Harness message when the model replied without tool calls (optional)."""

    def describe(self) -> dict[str, Any]:
        return {"agent": self.name}
