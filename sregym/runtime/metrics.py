"""Scrape GET /metrics periodically and append per-scrape deltas to the metrics store
(the same JSONL format the historical generator writes; query_metrics buckets by minute)."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone

from sregym import util
from sregym.generator.world import World

_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(-?[0-9.eE+-]+|NaN)\s*$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')
COUNTERS = {"http_requests_total", "http_request_duration_ms_sum", "http_request_duration_ms_count", "db_errors_total",
            "rate_limited_requests_total"}


def parse_prometheus(text: str) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for raw in text.splitlines():
        if not raw or raw.startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        name, labels, value = m.group(1), m.group(2) or "", m.group(3)
        try:
            v = float(value)
        except ValueError:
            continue
        lab = dict(_LABEL.findall(labels))
        out[(name, json.dumps(lab, sort_keys=True))] = v
    return out


class MetricsCollector(threading.Thread):
    def __init__(self, world: World, interval_s: float = 10.0):
        super().__init__(name="sregym-metrics", daemon=True)
        self.world = world
        self.interval = interval_s
        self._stop = threading.Event()
        self._prev: dict[tuple[str, str], float] = {}
        self._prev_start: float | None = None
        self.scrapes = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # first scrape establishes the baseline; nothing is written until deltas exist
        while not self._stop.is_set():
            try:
                self.scrape_once()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.interval)

    def scrape_once(self) -> None:
        ts = util.fmt_iso(datetime.now(timezone.utc))
        status, text = util.http_request("GET", f"{self.world.base_url}/metrics", timeout=3)
        rows: list[dict] = []
        if status != 200:
            rows.append({"ts": ts, "m": "up", "l": {}, "v": 0})
            self._prev = {}
            self._prev_start = None
        else:
            self.scrapes += 1
            cur = parse_prometheus(text)
            start = cur.get(("process_start_time_seconds", "{}"))
            restarted = self._prev_start is not None and start != self._prev_start
            rows.append({"ts": ts, "m": "up", "l": {}, "v": 1})
            baseline = not self._prev  # first successful scrape (after start or an outage): no deltas yet
            for (name, labels_json), v in cur.items():
                if name not in COUNTERS:
                    continue
                prev = self._prev.get((name, labels_json))
                if baseline:
                    delta = 0.0
                elif restarted or prev is None or v < prev:
                    delta = v  # new process / new label set / counter reset: everything is new
                else:
                    delta = v - prev
                if delta:
                    rows.append({"ts": ts, "m": name, "l": json.loads(labels_json), "v": round(delta, 3)})
            self._prev = cur
            self._prev_start = start
        with open(self.world.metrics_file, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
