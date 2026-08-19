"""Query the metrics store (historical series + live scrapes) as per-minute buckets."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sregym import util
from sregym.tools.base import Tool, ToolContext, ToolError, ToolResult

DERIVED = {
    "http_error_rate": "5xx responses / all responses per bucket (from http_requests_total)",
    "http_request_duration_ms_avg": "average latency per bucket (duration_ms_sum / duration_ms_count)",
}
GAUGES = {"up", "ledger_payments_total", "ledger_last_payment_age_seconds", "ledger_exporter_up"}
MAX_ROWS = 60
MAX_COLS = 8


def _load_rows(ctx: ToolContext, start: datetime, end: datetime) -> list[dict[str, Any]]:
    path = ctx.world.metrics_file
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                ts = util.parse_iso(r["ts"])
            except Exception:  # noqa: BLE001
                continue
            if start <= ts < end:
                r["_ts"] = ts
                rows.append(r)
    return rows


class QueryMetricsTool(Tool):
    name = "query_metrics"
    description = (
        "Query service metrics as a time series table (per-minute buckets by default). Call without a metric to list "
        "available metrics and labels. Metrics: http_requests_total{method,path,status}, http_request_duration_ms_sum/_count{path}, "
        "db_errors_total{db}, up, ledger_payments_total, ledger_last_payment_age_seconds (finance ledger exporter), "
        "plus derived http_error_rate and http_request_duration_ms_avg. "
        "Use group_by to split by a label (e.g. status, path) and filters to restrict (e.g. {\"path\": \"/checkout\"})."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "description": "Metric name (omit to list)."},
            "window_minutes": {"type": "integer", "minimum": 1, "maximum": 720, "description": "Look-back window ending now (default 30)."},
            "until": {"type": "string", "description": "End of the window as UTC 'HH:MM' or 'YYYY-MM-DD HH:MM' (default now)."},
            "step_minutes": {"type": "integer", "minimum": 1, "maximum": 60, "description": "Bucket size in minutes (default 1; auto-widened for long windows)."},
            "group_by": {"type": "string", "description": "Label to split columns by, e.g. status, path, method, db."},
            "filters": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Label filters, e.g. {\"path\": \"/checkout\", \"status\": \"500\"}."},
        },
        "required": [],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        now = datetime.now(timezone.utc)
        end = now + timedelta(minutes=1)
        if args.get("until"):
            from sregym.tools.read_logs import _parse_time

            end = _parse_time(str(args["until"]), now) + timedelta(minutes=1)
        window = int(args.get("window_minutes") or 30)
        window = max(1, min(720, window))
        start = end - timedelta(minutes=window)
        step = int(args.get("step_minutes") or 1)
        step = max(1, min(60, step))
        while window / step > MAX_ROWS:
            step = 5 if step < 5 else (15 if step < 15 else 60)
        rows = _load_rows(ctx, start, end)
        metric = args.get("metric")
        if not metric:
            return ToolResult(self._list(rows, start, end))
        metric = str(metric).strip()
        filters = {str(k): str(v) for k, v in (args.get("filters") or {}).items()}
        group_by = args.get("group_by") or None

        def bucket_of(ts: datetime) -> datetime:
            minutes = int((ts - start).total_seconds() // 60)
            return start + timedelta(minutes=(minutes // step) * step)

        def matches(r: dict) -> bool:
            return all(str(r["l"].get(k)) == v for k, v in filters.items())

        # collect per bucket, per group
        table: dict[datetime, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        counts: dict[datetime, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        if metric == "http_error_rate":
            num: dict[datetime, dict[str, float]] = defaultdict(lambda: defaultdict(float))
            den: dict[datetime, dict[str, float]] = defaultdict(lambda: defaultdict(float))
            for r in rows:
                if r["m"] != "http_requests_total" or not matches(r):
                    continue
                b, g = bucket_of(r["_ts"]), (str(r["l"].get(group_by, "-")) if group_by else "all")
                den[b][g] += r["v"]
                if str(r["l"].get("status", "")).startswith("5"):
                    num[b][g] += r["v"]
            for b in den:
                for g in den[b]:
                    table[b][g] = 100.0 * num[b][g] / den[b][g] if den[b][g] else 0.0
            unit, agg = "%", "5xx share"
        elif metric == "http_request_duration_ms_avg":
            s: dict[datetime, dict[str, float]] = defaultdict(lambda: defaultdict(float))
            c: dict[datetime, dict[str, float]] = defaultdict(lambda: defaultdict(float))
            for r in rows:
                if r["m"] not in ("http_request_duration_ms_sum", "http_request_duration_ms_count") or not matches(r):
                    continue
                b, g = bucket_of(r["_ts"]), (str(r["l"].get(group_by, "-")) if group_by else "all")
                (s if r["m"].endswith("_sum") else c)[b][g] += r["v"]
            for b in c:
                for g in c[b]:
                    table[b][g] = s[b][g] / c[b][g] if c[b][g] else 0.0
            unit, agg = "ms", "avg"
        else:
            known = {r["m"] for r in rows}
            if metric not in known and rows:
                raise ToolError(f"unknown metric {metric!r}; available: {', '.join(sorted(known | set(DERIVED)))}")
            for r in rows:
                if r["m"] != metric or not matches(r):
                    continue
                b, g = bucket_of(r["_ts"]), (str(r["l"].get(group_by, "-")) if group_by else "all")
                table[b][g] += r["v"]
                counts[b][g] += 1
            if metric in GAUGES:
                for b in table:
                    for g in table[b]:
                        table[b][g] = table[b][g] / counts[b][g]
                unit, agg = "", "avg"
            else:
                unit, agg = "", "sum"

        if not table:
            return ToolResult(f"metric={metric} window={window}m ({start:%Y-%m-%d %H:%M} -> {end - timedelta(minutes=1):%H:%M} UTC) filters={filters or '{}'}: no data")
        groups = sorted({g for b in table for g in table[b]}, key=lambda g: -sum(table[b].get(g, 0) for b in table))
        dropped = groups[MAX_COLS:]
        groups = groups[:MAX_COLS]
        head = f"metric={metric} ({agg}{(' ' + unit) if unit else ''}) window={window}m step={step}m" + \
            (f" group_by={group_by}" if group_by else "") + (f" filters={json.dumps(filters)}" if filters else "")
        colw = max(8, min(28, max((len(g) for g in groups), default=8)))
        lines = [head, "bucket(UTC)      " + " ".join(f"{g[:colw]:>{colw}}" for g in groups)]
        b = start
        current_bucket = None
        while b < end:
            vals = table.get(b, {})
            cells = []
            for g in groups:
                v = vals.get(g)
                cells.append(f"{'-' if v is None else (f'{v:.1f}' if unit or metric in GAUGES else f'{v:.0f}'):>{colw}}")
            marker = ""
            if b <= now < b + timedelta(minutes=step):
                marker = "  (in progress)"
                current_bucket = b
            lines.append(f"{b:%m-%d %H:%M}      " + " ".join(cells) + marker)
            b += timedelta(minutes=step)
        if current_bucket is not None:
            lines.append("('-' = no samples yet; the store is scraped every ~10s, so the in-progress bucket lags a little)")
        if dropped:
            lines.append(f"({len(dropped)} more {group_by} values omitted: {', '.join(dropped[:6])}{'...' if len(dropped) > 6 else ''})")
        return ToolResult("\n".join(lines))

    def _list(self, rows: list[dict], start: datetime, end: datetime) -> str:
        labels: dict[str, set[str]] = defaultdict(set)
        values: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        n = 0
        for r in rows:
            n += 1
            for k, v in r["l"].items():
                labels[r["m"]].add(k)
                values[r["m"]][k].add(str(v))
            labels.setdefault(r["m"], set())
        out = [f"metrics store: {n} samples in window {start:%Y-%m-%d %H:%M} -> {end - timedelta(minutes=1):%H:%M} UTC", ""]
        for m in sorted(labels):
            lab = ", ".join(f"{k}={{{', '.join(sorted(values[m][k])[:8])}{'...' if len(values[m][k]) > 8 else ''}}}" for k in sorted(labels[m]))
            out.append(f"- {m}" + (f"  labels: {lab}" if lab else ""))
        for m, d in DERIVED.items():
            out.append(f"- {m}  (derived: {d})")
        out.append("")
        out.append("Example: query_metrics(metric=\"http_error_rate\", window_minutes=60, group_by=\"path\")")
        return "\n".join(out)
