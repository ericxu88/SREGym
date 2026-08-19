"""Read a text file (with line numbers) from the host."""
from __future__ import annotations

from typing import Any

from sregym import util
from sregym.tools.base import Tool, ToolContext, ToolError, ToolResult, resolve_path

MAX_LINES = 200


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a text file (config, source, systemd/nginx/cron files, ...) with line numbers. Returns up to 200 lines per call; "
        "use start_line to page. For log files prefer read_logs; for .db files use run_shell with sqlite3."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the host root (or absolute within it)."},
            "start_line": {"type": "integer", "minimum": 1, "description": "First line to return (1-based, default 1)."},
            "max_lines": {"type": "integer", "minimum": 1, "maximum": MAX_LINES, "description": "Lines to return (default 200)."},
        },
        "required": ["path"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        full = resolve_path(ctx, args.get("path", ""))
        if full.is_dir():
            entries = sorted(full.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            listing = "\n".join(f"{'d' if p.is_dir() else '-'} {p.name}{'/' if p.is_dir() else ''}  {p.stat().st_size if p.is_file() else ''}" for p in entries)
            return ToolResult(f"{util.relpath(full, ctx.world.root)}/ is a directory:\n{listing}")
        data = full.read_bytes()
        if is_binary(data):
            hint = " (SQLite database: use run_shell with `sqlite3 <path> '<query>'`)" if data[:15] == b"SQLite format 3" else ""
            raise ToolError(f"{args.get('path')} is a binary file{hint}")
        lines = data.decode("utf-8", "replace").splitlines()
        start = max(1, int(args.get("start_line") or 1))
        n = max(1, min(MAX_LINES, int(args.get("max_lines") or MAX_LINES)))
        chunk = lines[start - 1:start - 1 + n]
        rel = util.relpath(full, ctx.world.root)
        header = f"{rel} ({len(lines)} lines) showing L{start}-L{start + len(chunk) - 1 if chunk else start}"
        if start - 1 + n < len(lines):
            header += f"; continue with start_line={start + n}"
        body = "\n".join(f"{start + i:>5}| {line}" for i, line in enumerate(chunk))
        return ToolResult(header + "\n" + body)
