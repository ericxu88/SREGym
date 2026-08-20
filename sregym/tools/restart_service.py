"""Service control (systemd stand-in) for the generated stack."""
from __future__ import annotations

from typing import Any

from sregym.tools.base import Tool, ToolContext, ToolError, ToolResult


class RestartServiceTool(Tool):
    name = "restart_service"
    description = (
        "Control the {service} process (like systemctl): action=restart (default), status, start or stop. "
        "Restarting re-reads configuration (.env) and re-imports the application code. Reports the new pid, "
        "whether the port accepts connections and the /health result."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service name (default {service})."},
            "action": {"type": "string", "enum": ["restart", "status", "start", "stop"], "description": "Default restart."},
        },
        "required": [],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        svc = ctx.world.naming.service
        service = str(args.get("service") or svc).strip()
        action = str(args.get("action") or "restart").strip().lower()
        if service != svc:
            if service in ("nginx", "cron", "crond", "prometheus"):
                return ToolResult(f"{service} is not managed on this host (the {svc} upstream is reached directly on 127.0.0.1)", is_error=True)
            raise ToolError(f"unknown service {service!r}; managed services: {svc}")
        sm = ctx.services
        if sm is None:
            raise ToolError("service manager unavailable in this context")
        if action == "status":
            return ToolResult(sm.status())
        if action == "stop":
            return ToolResult(sm.stop() + "\n" + sm.status())
        if action == "start":
            return ToolResult(sm.start() + "\n" + sm.status())
        if action == "restart":
            return ToolResult(sm.restart() + "\n" + sm.status())
        raise ToolError(f"unknown action {action!r}")
