"""Exact-match text replacement in a file (or create a new file)."""
from __future__ import annotations

import difflib
from typing import Any

from sregym import util
from sregym.tools.base import Tool, ToolContext, ToolError, ToolResult, resolve_path
from sregym.tools.read_file import is_binary

MAX_FILE_BYTES = 2 * 1024 * 1024


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Edit a text file by replacing an exact string. old_string must occur exactly once (include enough context to be unique) "
        "unless replace_all is true. To create a new file, pass an empty old_string with the full content in new_string. "
        "Returns a unified diff of the change. Binary files and log files cannot be edited."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the host root."},
            "old_string": {"type": "string", "description": "Exact text to replace (empty to create a new file)."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = args.get("path", "")
        old = args.get("old_string")
        new = args.get("new_string")
        if old is None or new is None:
            raise ToolError("old_string and new_string are required")
        full = resolve_path(ctx, path, must_exist=False)
        rel = util.relpath(full, ctx.world.root)
        if full.exists() and full.is_dir():
            raise ToolError(f"{rel} is a directory")
        if full.suffix == ".log" or "/logs/" in f"/{rel}" or rel.startswith("var/log/"):
            raise ToolError("log files are read-only (use read_logs)")
        if not full.exists():
            if old != "":
                raise ToolError(f"{rel} does not exist; to create it pass old_string=\"\"")
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(new)
            return ToolResult(f"created {rel} ({len(new.splitlines())} lines)")
        data = full.read_bytes()
        if is_binary(data):
            raise ToolError(f"{rel} is a binary file and cannot be edited")
        if len(data) > MAX_FILE_BYTES:
            raise ToolError(f"{rel} is too large to edit ({len(data)} bytes)")
        text = data.decode("utf-8")
        if old == "":
            raise ToolError(f"{rel} exists; old_string must be non-empty to edit an existing file")
        count = text.count(old)
        if count == 0:
            raise ToolError(f"old_string not found in {rel}")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"old_string occurs {count} times in {rel}; include more context or set replace_all=true")
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        full.write_text(updated)
        diff = difflib.unified_diff(text.splitlines(), updated.splitlines(), fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=3)
        return ToolResult(f"edited {rel} ({count} replacement{'s' if count != 1 else ''}):\n" + "\n".join(diff))
