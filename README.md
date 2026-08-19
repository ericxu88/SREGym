# SREGym

A procedurally-generated incident-response RL environment for LLM agents.

Every episode generates a small but complete production stack, injects a known fault
from a template library, gives the agent on-call tools, and deterministically verifies
whether the agent fixed the true root cause.

Work in progress. Build order: world generator + app -> fault injection + task prompt ->
tools -> verifier -> agent harness -> CLI.
