"""Terminal tool: the agent declares the incident resolved."""
from __future__ import annotations

from typing import Any

from sregym.tools.base import Tool, ToolContext, ToolResult


class ResolveIncidentTool(Tool):
    name = "resolve_incident"
    description = (
        "Declare the incident resolved and end the session. Provide a short postmortem: what broke, the root cause, "
        "what you changed, and how you verified recovery. Only call this after you have verified the fix."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Postmortem summary (a few sentences)."},
            "root_cause": {"type": "string", "description": "One-line root cause statement."},
        },
        "required": ["summary"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        summary = str(args.get("summary", "")).strip()
        root_cause = str(args.get("root_cause", "")).strip()
        return ToolResult(
            "Incident marked resolved. Postmortem recorded." + (f"\nroot_cause: {root_cause}" if root_cause else ""),
            meta={"terminal": True, "summary": summary, "root_cause": root_cause},
        )
