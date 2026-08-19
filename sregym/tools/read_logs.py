"""Paginated, cursor-based log reader -- the heart of the investigation loop.

At most 50 lines per call. Supports server-side ``grep`` (regex), ``since``/``until``
time filters and reading from the tail. Cursors are opaque strings that continue a
scan in the same direction with the same filters.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sregym import util
from sregym.tools.base import Tool, ToolContext, ToolError, ToolResult, resolve_path

MAX_LINES = 50
MAX_LINE_CHARS = 400

_TS_PATTERNS = [
    (re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})"), "%Y-%m-%d %H:%M:%S"),  # app/deploy/cron
    (re.compile(r"^(\d{4}/\d{2}/\d{2}) (\d{2}:\d{2}:\d{2})"), "%Y/%m/%d %H:%M:%S"),  # nginx error
    (re.compile(r"\[(\d{2}/[A-Za-z]{3}/\d{4}):(\d{2}:\d{2}:\d{2})"), "%d/%b/%Y %H:%M:%S"),  # nginx access
]


def _line_ts(line: str) -> datetime | None:
    for pat, fmt in _TS_PATTERNS:
        m = pat.search(line[:60])
        if m:
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)}", fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


def _parse_time(text: str, ref: datetime | None) -> datetime:
    """Accept 'HH:MM', 'HH:MM:SS', 'YYYY-MM-DD HH:MM[:SS]', ISO-8601. Bare times take the
    date of the log's last line (or the previous day if that would be in its future)."""
    text = text.strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(text, fmt)
            base = (ref or datetime.now(timezone.utc)).replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            if ref and base > ref + timedelta(minutes=1):
                base -= timedelta(days=1)
            return base
        except ValueError:
            pass
    raise ToolError(f"cannot parse time {text!r}; use HH:MM, HH:MM:SS or 'YYYY-MM-DD HH:MM:SS' (UTC)")


def _encode_cursor(d: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(s: str) -> dict[str, Any]:
    try:
        pad = "=" * (-len(s) % 4)
        return json.loads(base64.urlsafe_b64decode(s + pad).decode())
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"invalid cursor: {e}") from e


class ReadLogsTool(Tool):
    name = "read_logs"
    max_output_chars = MAX_LINES * (MAX_LINE_CHARS + 40) + 1000  # never clip a full page
    description = (
        "Read a log file page by page (max 50 lines per call). Call with no path to list available log files. "
        "Use grep (regex, case-sensitive; prefix (?i) to ignore case), since/until (UTC 'HH:MM' or 'YYYY-MM-DD HH:MM:SS') and tail=true to focus; "
        "pass the returned next_cursor to continue in the same direction with the same filters. "
        "Log files: checkout-service/logs/app.log (application + access log), checkout-service/logs/deploy.log, "
        "checkout-service/logs/cron.log, var/log/nginx/access.log, var/log/nginx/error.log."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Log file path relative to the host root (omit to list log files)."},
            "cursor": {"type": "string", "description": "Opaque cursor from a previous call; continues the same scan. Other filter args are ignored when a cursor is given."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LINES, "description": "Lines to return (default and max 50)."},
            "grep": {"type": "string", "description": "Regex; only lines matching are returned (case-sensitive; use (?i) to ignore case)."},
            "since": {"type": "string", "description": "Only lines at/after this UTC time (HH:MM, HH:MM:SS or full timestamp)."},
            "until": {"type": "string", "description": "Only lines at/before this UTC time."},
            "tail": {"type": "boolean", "description": "Start from the end of the file (most recent lines) and page backwards."},
        },
        "required": [],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if args.get("cursor"):
            state = _decode_cursor(args["cursor"])
            path = state["p"]
        else:
            path = args.get("path")
            if not path:
                return ToolResult(self._list(ctx))
            state = None
        full = resolve_path(ctx, path)
        if full.is_dir():
            return ToolResult(self._list(ctx, full))
        limit = int(args.get("limit") or MAX_LINES)
        limit = max(1, min(MAX_LINES, limit))
        lines = full.read_text(errors="replace").splitlines()
        total = len(lines)
        first_ts = next((t for t in map(_line_ts, lines[:200]) if t), None)
        last_ts = next((t for t in map(_line_ts, reversed(lines[-200:])) if t), None)

        if state is None:
            grep = args.get("grep") or None
            since = _parse_time(args["since"], last_ts) if args.get("since") else None
            until = _parse_time(args["until"], last_ts) if args.get("until") else None
            direction = "back" if args.get("tail") else "fwd"
            index = total if direction == "back" else 0
            state = {"p": util.relpath(full, ctx.world.root), "i": index, "d": direction, "g": grep,
                     "s": util.fmt_iso(since) if since else None, "u": util.fmt_iso(until) if until else None}
        grep = state.get("g")
        since = util.parse_iso(state["s"]) if state.get("s") else None
        until = util.parse_iso(state["u"]) if state.get("u") else None
        direction = state["d"]
        index = max(0, min(total, int(state["i"])))
        try:
            rx = re.compile(grep) if grep else None
        except re.error as e:
            raise ToolError(f"bad grep regex: {e}") from e

        # compute matching line indexes (with timestamp inheritance for continuation lines)
        matches: list[int] = []
        cur_ts: datetime | None = None
        for i, line in enumerate(lines):
            ts = _line_ts(line)
            if ts is not None:
                cur_ts = ts
            if since and (cur_ts is None or cur_ts < since):
                continue
            if until and cur_ts is not None and cur_ts > until:
                continue
            if rx and not rx.search(line):
                continue
            matches.append(i)

        if direction == "fwd":
            sel = [i for i in matches if i >= index][:limit]
            next_index = (sel[-1] + 1) if sel else index
            remaining = len([i for i in matches if i >= next_index])
        else:
            before = [i for i in matches if i < index]
            sel = before[-limit:]
            next_index = sel[0] if sel else index
            remaining = len([i for i in matches if i < next_index])

        rel = state["p"]
        size = full.stat().st_size
        header = [f"{rel}  ({total} lines, {size / 1024:.0f} KB" + (f", {first_ts:%Y-%m-%d %H:%M:%S} -> {last_ts:%Y-%m-%d %H:%M:%S} UTC" if first_ts and last_ts else "") + ")"]
        filt = []
        if grep:
            filt.append(f'grep="{grep}"')
        if since:
            filt.append(f"since={since:%Y-%m-%d %H:%M:%S}")
        if until:
            filt.append(f"until={until:%Y-%m-%d %H:%M:%S}")
        filt_s = (" ".join(filt) + " -> ") if filt else ""
        if sel:
            pos = f"L{sel[0] + 1}-L{sel[-1] + 1}"
        else:
            pos = "none"
        header.append(f"{filt_s}{len(matches)} matching lines; showing {len(sel)} ({pos}), reading {'backwards (older)' if direction == 'back' else 'forwards (newer)'}; {remaining} more in this direction")
        new_state = dict(state, i=next_index)
        if remaining:
            header.append(f"next_cursor: {_encode_cursor(new_state)}")
        else:
            header.append("next_cursor: (none - end of results in this direction)")
        if direction == "back" and not args.get("cursor"):
            header.append(f"live_cursor (lines appended after this read): {_encode_cursor(dict(state, i=total, d='fwd'))}")
        body = []
        for i in sel:
            text = lines[i]
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS] + " ...[line truncated]"
            body.append(f"L{i + 1:<6} {text}")
        return ToolResult("\n".join(header) + "\n--\n" + ("\n".join(body) if body else "(no lines)"))

    def _list(self, ctx: ToolContext, only_dir: Path | None = None) -> str:
        world = ctx.world
        rows = []
        for p in world.log_files():
            if only_dir and not util.is_within(p, only_dir):
                continue
            lines = p.read_text(errors="replace").splitlines()
            first = next((t for t in map(_line_ts, lines[:200]) if t), None)
            last = next((t for t in map(_line_ts, reversed(lines[-200:])) if t), None)
            span = f"{first:%Y-%m-%d %H:%M} -> {last:%H:%M} UTC" if first and last else "-"
            rows.append(f"{util.relpath(p, world.root):<40} {len(lines):>7} lines  {p.stat().st_size / 1024:>7.0f} KB  {span}")
        return "Available log files (path, lines, size, time span):\n" + "\n".join(rows) + \
            "\n\nUse read_logs(path=..., tail=true) for the most recent lines, or grep/since/until to filter."
