"""Model adapters. Each adapter implements :class:`AgentAdapter` from ``base``."""
from __future__ import annotations

from sregym.harness.agents.base import AgentAdapter, AgentTurn, ToolCall


def make_agent(kind: str, **kwargs) -> AgentAdapter:
    """Factory: ``anthropic`` (Claude via the Anthropic API), ``scripted`` (deterministic reference solver).

    Adding a provider = one new module implementing AgentAdapter + an entry here.
    """
    if kind == "anthropic":
        from sregym.harness.agents.anthropic_adapter import AnthropicAdapter

        return AnthropicAdapter(**kwargs)
    if kind == "scripted":
        from sregym.harness.agents.scripted import ScriptedAgent

        return ScriptedAgent(**{k: v for k, v in kwargs.items() if k in ("mode",)})
    raise ValueError(f"unknown agent kind {kind!r} (available: anthropic, scripted)")


__all__ = ["AgentAdapter", "AgentTurn", "ToolCall", "make_agent"]
