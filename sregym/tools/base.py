"""Tool interface shared by all agent-facing tools.

Each tool declares an Anthropic-style JSON schema (``input_schema``) so adapters for
other providers can translate it mechanically. Tools receive a :class:`ToolContext`
(world + service manager) and return a :class:`ToolResult` (text + error flag).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sregym import util
from sregym.generator.world import SERVICE_NAME, World

if TYPE_CHECKING:  # pragma: no cover
    from sregym.runtime.services import ServiceManager

MAX_OUTPUT_CHARS = 8000


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)  # e.g. {"terminal": True}


@dataclass
class ToolContext:
    world: World
    services: "ServiceManager | None" = None
    max_output_chars: int = MAX_OUTPUT_CHARS
    _allowed_scripts: dict[str, str] | None = field(default=None, repr=False)

    @property
    def allowed_scripts(self) -> dict[str, str]:
        """Repo scripts the shell may execute: root-relative path -> generation-time sha256 (from the manifest).
        Only unmodified, generation-time scripts run, so the agent cannot smuggle arbitrary code through them."""
        if self._allowed_scripts is None:
            try:
                files = self.world.load_manifest().get("files", {})
            except (OSError, ValueError):
                files = {}
            self._allowed_scripts = {rel: sha for rel, sha in files.items()
                                     if rel.startswith(f"{SERVICE_NAME}/scripts/") and rel.endswith(".py")}
        return self._allowed_scripts


class ToolError(Exception):
    """Raised by tools for user-facing errors (bad args, sandbox violations)."""


class Tool:
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    max_output_chars: int = MAX_OUTPUT_CHARS  # per-tool override of the result size clip

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # pragma: no cover - interface
        raise NotImplementedError

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def resolve_path(ctx: ToolContext, path: str, must_exist: bool = True) -> Path:
    """Resolve an agent-supplied path inside the host root (the control plane lives outside it)."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path is required")
    p = Path(path.strip())
    if str(p).startswith("~"):
        raise ToolError("home-relative paths are not allowed")
    full = (p if p.is_absolute() else ctx.world.root / p).resolve()
    if not util.is_within(full, ctx.world.root):
        raise ToolError(f"path {path!r} is outside the host filesystem you have access to ({ctx.world.root})")
    if must_exist and not full.exists():
        raise ToolError(f"no such file or directory: {path}")
    return full


def clip(text: str, limit: int) -> str:
    return util.truncate_text(text, limit, marker="\n... [output truncated: {n} chars omitted; narrow your query] ...\n")


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self.tools = {t.name: t for t in tools}

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self.tools.values()]

    def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"unknown tool {name!r}; available: {', '.join(self.tools)}", is_error=True)
        try:
            result = tool.run(args or {}, ctx)
        except ToolError as e:
            return ToolResult(f"error: {e}", is_error=True)
        except Exception as e:  # noqa: BLE001 - never crash the episode on a tool bug
            return ToolResult(f"tool {name} failed: {type(e).__name__}: {e}", is_error=True)
        result.content = clip(result.content, max(ctx.max_output_chars, tool.max_output_chars))
        return result


def default_registry() -> ToolRegistry:
    from sregym.tools.edit_file import EditFileTool
    from sregym.tools.query_metrics import QueryMetricsTool
    from sregym.tools.read_file import ReadFileTool
    from sregym.tools.read_logs import ReadLogsTool
    from sregym.tools.resolve_incident import ResolveIncidentTool
    from sregym.tools.restart_service import RestartServiceTool
    from sregym.tools.run_shell import RunShellTool

    return ToolRegistry([
        ReadLogsTool(), QueryMetricsTool(), ReadFileTool(), EditFileTool(), RunShellTool(),
        RestartServiceTool(), ResolveIncidentTool(),
    ])
