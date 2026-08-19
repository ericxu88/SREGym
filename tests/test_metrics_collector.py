"""Metrics collector: counter deltas survive transient scrape failures and detect restarts."""
from __future__ import annotations

import json

from sregym.runtime import metrics as m


def _prom(start: int, checkout_500: int, up: bool = True) -> tuple[int, str]:
    if not up:
        return 0, "connection error"
    return 200, (
        f'http_requests_total{{method="POST",path="/checkout",status="500"}} {checkout_500}\n'
        f"process_start_time_seconds {start}\n"
    )


def test_deltas_continue_across_failed_scrape_and_reset_on_restart(tmp_path, monkeypatch):
    class W:  # minimal stand-in for World
        base_url = "http://127.0.0.1:1"
        metrics_file = tmp_path / "series.jsonl"
        ledger_db = tmp_path / "no-ledger.db"  # exporter reports ledger_exporter_up=0 for a missing file

    responses = iter([_prom(100, 10), _prom(100, 25), _prom(100, 0, up=False), _prom(100, 40), _prom(200, 3)])
    monkeypatch.setattr(m.util, "http_request", lambda *a, **k: next(responses))
    c = m.MetricsCollector(W())  # type: ignore[arg-type]
    for _ in range(5):
        c.scrape_once()
    rows = [json.loads(l) for l in W.metrics_file.read_text().splitlines()]
    ups = [r["v"] for r in rows if r["m"] == "up"]
    reqs = [r["v"] for r in rows if r["m"] == "http_requests_total"]
    assert ups == [1, 1, 0, 1, 1]
    # baseline (no delta), +15, (down), +15 across the outage, restart -> counter value counts as new
    assert reqs == [15, 15, 3]
