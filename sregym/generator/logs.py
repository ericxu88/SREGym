"""Generate the historical evidence trail: app log, nginx logs, deploy log, cron log
and the metrics series -- for the window ``[world.history_start, world.now)``.

Everything is derived from one simulated request stream so the artifacts agree with
each other (and with the database: successful checkouts in the window are inserted as
real orders/payments). If an :class:`IncidentProfile` is given, the stream reproduces
the incident: a deploy restart in the app log, then failing endpoints with tracebacks
buried in normal traffic.
"""
from __future__ import annotations

import json
import math
import random
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sregym import util
from sregym.generator import traffic_profile as tp
from sregym.generator.world import World

if TYPE_CHECKING:  # pragma: no cover
    from sregym.faults.base import IncidentProfile


@dataclass
class _Event:
    ts: datetime
    lines: list[str]  # app.log lines (already formatted)
    seq: int


@dataclass
class _Sim:
    rng: random.Random
    world: World
    incident: IncidentProfile | None
    user_ids: list[int]
    products: list[dict]
    max_order_id: int
    tb: dict[str, list[str]]  # handler -> traceback template lines (without prefix)
    events: list[_Event] = field(default_factory=list)
    nginx_access: list[tuple[datetime, str]] = field(default_factory=list)
    nginx_error: list[tuple[datetime, str]] = field(default_factory=list)
    new_orders: list[tuple] = field(default_factory=list)
    new_items: list[tuple] = field(default_factory=list)
    new_payments: list[tuple] = field(default_factory=list)  # -> the real ledger
    new_settlements: list[tuple] = field(default_factory=list)  # gateway settlement webhooks that were accepted
    settlement_times: list[datetime] = field(default_factory=list)
    settlement_base_count: int = 0
    settlement_base_last: datetime | None = None
    diverted_payments: list[tuple] = field(default_factory=list)  # -> incident.extra["payments_db"] (e.g. a stale snapshot)
    ledger_payment_times: list[datetime] = field(default_factory=list)  # payments that reached the real ledger
    ledger_base_count: int = 0
    ledger_base_last: datetime | None = None
    metrics: dict[str, dict] = field(default_factory=dict)  # minute-iso -> aggregates
    rate_counts: dict[tuple[int, str], int] = field(default_factory=dict)  # (user, minute) -> checkout attempts
    seq: int = 0
    stats: dict[str, Any] = field(default_factory=lambda: {"requests": 0, "errors": 0, "incident_requests": 0, "incident_errors": 0})

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    @property
    def pkg(self) -> str:
        return self.world.naming.package


def _fmt(ts: datetime, level: str, logger: str, msg: str) -> str:
    return f"{util.fmt_log_ts(ts)} {level:<7} {logger} {msg}"


def _find_line(lines: list[str], needle: str, start: int = 0) -> int:
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i + 1
    raise ValueError(f"line containing {needle!r} not found")


def _traceback_templates(repo: Path, pkg: str) -> dict[str, list[str]]:
    """Traceback text per failing handler, with line numbers taken from the real source."""
    main = (repo / pkg / "main.py").read_text().splitlines()
    db = (repo / pkg / "db.py").read_text().splitlines()
    l_call_next = _find_line(main, "response = await call_next(request)")
    l_session = _find_line(db, "conn = _connect(url)")
    l_connect = _find_line(db, "conn = sqlite3.connect(")
    connect_src = db[l_connect - 1].strip()
    core_src = 'yield from _session(settings.database_url, "core")'
    ledger_src = 'yield from _session(settings.ledger_database_url, "ledger")'
    l_core = _find_line(db, core_src)
    l_ledger = _find_line(db, ledger_src)

    def handler_with(handler: str, needle: str) -> int:
        start = _find_line(main, f"def {handler}(")
        return _find_line(main, needle, start)

    def build(handler: str, needle: str, db_func: str, l_db: int, db_src: str) -> list[str]:
        return [
            "Traceback (most recent call last):",
            f'  File "{pkg}/main.py", line {l_call_next}, in observe_requests',
            "    response = await call_next(request)",
            f'  File "{pkg}/main.py", line {handler_with(handler, needle)}, in {handler}',
            f"    {needle}",
            f'  File "{pkg}/db.py", line {l_db}, in {db_func}',
            f"    {db_src}",
            f'  File "{pkg}/db.py", line {l_session}, in _session',
            "    conn = _connect(url)",
            f'  File "{pkg}/db.py", line {l_connect}, in _connect',
            f"    {connect_src}",
        ]

    out = {}
    for handler in ("get_order", "list_orders", "get_user", "list_users", "checkout"):
        out[handler] = build(handler, "with core_db() as conn:", "core_db", l_core, core_src)
    out["checkout_ledger"] = build("checkout", "with ledger_db() as ledger:", "ledger_db", l_ledger, ledger_src)

    # SQL errors raised by a statement inside the handler (e.g. a column the schema does not have yet):
    # the only app frames are the middleware and the handler's execute line.
    def build_sql(handler: str, needle: str) -> list[str]:
        lineno = handler_with(handler, needle)
        return [
            "Traceback (most recent call last):",
            f'  File "{pkg}/main.py", line {l_call_next}, in observe_requests',
            "    response = await call_next(request)",
            f'  File "{pkg}/main.py", line {lineno}, in {handler}',
            f"    {main[lineno - 1].strip()}",
        ]

    for handler, needle in (("checkout", 'cur = conn.execute(f"INSERT INTO orders ('),
                            ("checkout_ledger_sql", 'pcur = ledger.execute('),
                            ("get_order", 'order = _row(conn.execute(f"SELECT {ORDER_COLUMNS} FROM orders WHERE id = ?"'),
                            ("list_orders", "rows = conn.execute("), ("get_user", 'user = _row(conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = ?"'),
                            ("list_users", "rows = conn.execute(")):
        name = "checkout" if handler == "checkout_ledger_sql" else handler
        try:
            out[f"sql:{handler}"] = build_sql(name, needle)
        except ValueError:
            pass
    return out


def _load_refs(world: World) -> tuple[list[int], list[dict], int, int, datetime | None]:
    conn = sqlite3.connect(f"file:{world.core_db}?mode=ro", uri=True)
    try:
        user_ids = [r[0] for r in conn.execute("SELECT id FROM users ORDER BY id")]
        products = [{"id": r[0], "sku": r[1], "price_cents": r[2]} for r in conn.execute("SELECT id, sku, price_cents FROM products WHERE active = 1")]
        max_order = conn.execute("SELECT COALESCE(MAX(id), 0) FROM orders").fetchone()[0]
    finally:
        conn.close()
    ledger = sqlite3.connect(f"file:{world.ledger_db}?mode=ro", uri=True)
    try:
        count, last = ledger.execute("SELECT COUNT(*), MAX(created_at) FROM payments").fetchone()
        s_count, s_last = ledger.execute("SELECT COUNT(*), MAX(settled_at) FROM settlements").fetchone()
    finally:
        ledger.close()
    return (user_ids, products, max_order, int(count), (util.parse_iso(last) if last else None),
            int(s_count), (util.parse_iso(s_last) if s_last else None))


def _bump_metric(sim: _Sim, ts: datetime, name: str, labels: dict[str, str], value: float) -> None:
    minute = ts.replace(second=0, microsecond=0)
    bucket = sim.metrics.setdefault(util.fmt_iso(minute), {})
    key = name + "|" + json.dumps(labels, sort_keys=True)
    bucket[key] = bucket.get(key, 0) + value


def _record_request(sim: _Sim, ts: datetime, method: str, template: str, path: str, status: int,
                    latency: float, extra: dict[str, Any], error: str | None, tb_key: str | None,
                    warn_lines: list[str] | None = None, ua: str | None = None, ip: str | None = None) -> None:
    rng = sim.rng
    nm = sim.world.naming
    path = nm.route(path)
    label = nm.route(template)
    req_id = "%08x" % rng.getrandbits(32)
    lines: list[str] = []
    for w in warn_lines or []:
        lines.append(_fmt(ts, "WARNING", f"{sim.pkg}.db", w))
    if tb_key:
        for tb_line in sim.tb[tb_key] + [error]:
            lines.append(_fmt(ts, "ERROR", f"{sim.pkg}.app", f"req={req_id} {tb_line}"))
    msg = f"req={req_id} {method} {path} {status} {latency:.0f}ms"
    if extra:
        msg += " " + " ".join(f"{k}={v}" for k, v in extra.items())
    if error:
        msg += f' error="{error}"'
    level = "ERROR" if status >= 500 else "INFO"
    lines.append(_fmt(ts, level, f"{sim.pkg}.access", msg))
    sim.events.append(_Event(ts, lines, sim.next_seq()))
    # metrics
    _bump_metric(sim, ts, "http_requests_total", {"method": method, "path": label, "status": str(status)}, 1)
    _bump_metric(sim, ts, "http_request_duration_ms_sum", {"path": label}, round(latency, 3))
    _bump_metric(sim, ts, "http_request_duration_ms_count", {"path": label}, 1)
    if error and sim.incident:
        _bump_metric(sim, ts, "db_errors_total", {"db": sim.incident.broken_db}, 1)
    # nginx access log (health probes are access_log off in the nginx config)
    if template != "/health":
        ua = ua or rng.choice(tp.USER_AGENTS)
        size = {200: rng.randint(180, 620), 201: rng.randint(150, 220), 400: rng.randint(40, 90), 401: 39, 404: 27,
                422: rng.randint(90, 160), 429: 32, 500: 98, 503: rng.randint(150, 220)}.get(status, 100)
        ip = ip or ("10.0.4.12" if template == "/metrics" else tp.fake_client_ip(rng))
        sim.nginx_access.append((ts, f'{ip} - - [{ts.strftime("%d/%b/%Y:%H:%M:%S +0000")}] "{method} {path} HTTP/1.1" {status} {size} "-" "{ua}"'))
    sim.stats["requests"] += 1
    if status >= 500:
        sim.stats["errors"] += 1
    if sim.incident and ts >= sim.incident.incident_at:
        sim.stats["incident_requests"] += 1
        if status >= 500:
            sim.stats["incident_errors"] += 1


def _checkout_attempt(sim: _Sim, ts: datetime, user_id: int, failing: bool, core_broken: bool,
                      override: dict | None, slow_window: bool) -> None:
    rng, inc = sim.rng, sim.incident
    slow = slow_window and rng.random() < 0.2
    latency = tp.latency_ms(rng, "/checkout", slow=slow)
    extra: dict[str, Any] = {"user": user_id}
    status, error, tb_key = 200, None, None
    # per-user fixed-window rate limiter (mirrors the app's _RateLimiter; healthy config allows bursts)
    limit = 600
    rl = inc.extra.get("rate_limit") if inc else None
    if rl and ts >= util.parse_iso(rl["since"]):
        limit = int(rl["limit"])
    minute_key = (user_id, ts.strftime("%Y-%m-%dT%H:%M"))
    sim.rate_counts[minute_key] = sim.rate_counts.get(minute_key, 0) + 1
    if sim.rate_counts[minute_key] > limit:
        sim.events.append(_Event(ts, [_fmt(ts, "WARNING", f"{sim.pkg}.ratelimit", f"user={user_id} exceeded {limit} checkouts/min")], sim.next_seq()))
        _bump_metric(sim, ts, "rate_limited_requests_total", {}, 1)
        _record_request(sim, ts, "POST", "/checkout", "/checkout", 429, tp.latency_ms(rng, "/orders"), extra, None, None)
        return
    r = rng.random()
    if failing:
        status = 500
        error = override["error"] if override else inc.error_message
        tb_key = override["tb"] if override else ("checkout" if core_broken else "checkout_ledger")
        latency = tp.latency_ms(rng, "/orders")  # fails fast
        if override and override.get("latency_ms"):
            latency = float(override["latency_ms"]) + rng.uniform(0, 30)
    elif r < 0.012:
        status = 422
        extra = {}
    elif r < 0.03:
        status = 400
    elif r < 0.04:
        status = 404
        extra["user"] = rng.randint(sim.user_ids[-1] + 1, sim.user_ids[-1] + 5000)
    else:
        status = 201
        sim.max_order_id += 1
        order_id = sim.max_order_id
        extra["order"] = order_id
        items = rng.sample(sim.products, k=min(len(sim.products), rng.choice([1, 1, 1, 2, 2, 3])))
        total = 0
        created = util.fmt_iso(ts)
        for p_ in items:
            qty = rng.choice([1, 1, 1, 2, 3])
            total += p_["price_cents"] * qty
            sim.new_items.append((order_id, p_["id"], qty, p_["price_cents"]))
        sim.new_orders.append((order_id, user_id, "confirmed", total, "USD", created, created))
        payment = (order_id, user_id, total, "USD", rng.choice(["card"] * 7 + ["paypal"] * 2 + ["apple_pay"]),
                   "captured", "ch_%016x" % rng.getrandbits(64), created)
        diverted = bool(inc) and ts >= inc.incident_at and bool(inc.extra.get("payments_db"))
        if diverted:
            sim.diverted_payments.append(payment)
        else:
            sim.new_payments.append(payment)
            sim.ledger_payment_times.append(ts)
        _gateway_webhook(sim, ts, order_id, total, payment[6], diverted)
    warn_lines = [f"slow database transaction ({latency:.0f}ms) db=core"] if slow else None
    _record_request(sim, ts, "POST", "/checkout", "/checkout", status, latency, extra, error, tb_key, warn_lines)


def _gateway_webhook(sim: _Sim, ts: datetime, order_id: int, amount: int, ref: str, diverted: bool) -> None:
    """The gateway pushes a signed settlement confirmation shortly after each capture."""
    rng = sim.rng
    at = ts + timedelta(seconds=rng.uniform(2, 20))
    if at >= sim.world.now:
        return  # would land after "now"
    inc = sim.incident
    if inc and -4.0 < (at - inc.restart_at).total_seconds() < 1.0:
        return  # would land in the restart's down-gap (the gateway retries later)
    key = "POST /webhooks/payments"
    rejected = bool(inc) and key in inc.failing_endpoints and at >= inc.incident_at
    latency = tp.latency_ms(rng, "/webhooks/payments")
    if rejected:
        sim.events.append(_Event(at, [_fmt(at, "WARNING", f"{sim.pkg}.webhooks",
                                          f"webhook signature mismatch ({rng.randint(148, 196)} bytes dropped)")],
                                 sim.next_seq()))
        _bump_metric(sim, at, "webhook_signature_failures_total", {}, 1)
        _record_request(sim, at, "POST", "/webhooks/payments", "/webhooks/payments", 401, latency, {}, None, None,
                        ua="PaymentsGateway-Webhooks/2.4")
        return
    settled = util.fmt_iso(at)
    sim.new_settlements.append((ref, order_id, amount, settled))
    if not diverted:
        sim.settlement_times.append(at)
    _record_request(sim, at, "POST", "/webhooks/payments", "/webhooks/payments", 200, latency, {"ref": ref}, None, None,
                    ua="PaymentsGateway-Webhooks/2.4")


def _endpoint_state(sim: _Sim, ts: datetime, key: str) -> tuple[bool, dict | None]:
    """(failing, error_override) for an endpoint at a time. Each endpoint override may carry its own
    ``since`` (composed faults start at different times); the lock burst may be scoped to ``endpoints``."""
    inc = sim.incident
    if not inc or key not in inc.failing_endpoints:
        return False, None
    override = (inc.extra.get("endpoint_errors") or {}).get(key)
    since = util.parse_iso(override["since"]) if override and override.get("since") else inc.incident_at
    failing = ts >= since
    burst = inc.extra.get("lock_burst")
    if failing and burst and key in burst.get("endpoints", inc.failing_endpoints):
        burst_since = util.parse_iso(burst["since"]) if burst.get("since") else inc.incident_at
        phase = (ts - burst_since).total_seconds() % float(burst["period_s"])
        failing = burst["offset_s"] <= phase < burst["offset_s"] + burst["duration_s"]
    return failing, (override if failing else None)


def _simulate_request(sim: _Sim, ts: datetime, slow_window: bool) -> None:
    rng, inc = sim.rng, sim.incident
    method, template = tp.pick_endpoint(rng)
    key = f"{method} {template}"
    failing, override = _endpoint_state(sim, ts, key)
    core_broken = bool(inc) and inc.broken_db == "core"
    slow = slow_window and template != "/checkout" and rng.random() < 0.4
    latency = tp.latency_ms(rng, template, slow=slow)
    warn = [f"slow database transaction ({latency:.0f}ms) db=core"] if slow else None
    extra: dict[str, Any] = {}
    error = tb_key = None
    status = 200

    if template == "/checkout":
        user_id = rng.choice(sim.user_ids)
        _checkout_attempt(sim, ts, user_id, failing, core_broken, override, slow_window)
        if rng.random() < tp.BURST_PROB:  # double-click / client retry / split cart
            burst_at = ts
            for _ in range(rng.randint(*tp.BURST_EXTRA)):
                burst_at += timedelta(seconds=rng.uniform(1.2, 9.0))
                if burst_at >= sim.world.now:
                    break
                b_failing, b_override = _endpoint_state(sim, burst_at, key)
                _checkout_attempt(sim, burst_at, user_id, b_failing, core_broken, b_override, slow_window)
        return
    elif template == "/orders/{order_id}":
        if rng.random() < 0.05:
            order_id = rng.randint(sim.max_order_id + 1, sim.max_order_id + 20000)
            status = 404
        else:
            order_id = rng.randint(max(1, sim.max_order_id - 4000), sim.max_order_id)
        extra["order"] = order_id
        path = f"/orders/{order_id}"
        if failing:
            status, error, tb_key = 500, (override["error"] if override else inc.error_message), (override["tb"] if override else "get_order")
    elif template == "/orders":
        user_id = rng.choice(sim.user_ids)
        extra["user"] = user_id
        path = f"/orders?user_id={user_id}" + rng.choice(["", "", "&limit=10", "&status=confirmed", "&limit=50&offset=0"])
        if failing:
            status, error, tb_key = 500, (override["error"] if override else inc.error_message), (override["tb"] if override else "list_orders")
    elif template == "/users/{user_id}":
        if rng.random() < 0.02:
            user_id = rng.randint(sim.user_ids[-1] + 1, sim.user_ids[-1] + 5000)
            status = 404
        else:
            user_id = rng.choice(sim.user_ids)
        extra["user"] = user_id
        path = f"/users/{user_id}"
        if failing:
            status, error, tb_key = 500, (override["error"] if override else inc.error_message), (override["tb"] if override else "get_user")
    else:  # /users
        path = "/users?" + rng.choice(["limit=20", "limit=50", "limit=20&offset=20", "limit=100&offset=100"])
        if failing:
            status, error, tb_key = 500, (override["error"] if override else inc.error_message), (override["tb"] if override else "list_users")
    _record_request(sim, ts, method, template, path, status, latency, extra, error, tb_key, warn)


def _restart_sequence(sim: _Sim, restart_at: datetime, commit_sha: str, version: str, n_keys: int,
                      warnings: list[str], old_pid: int, new_pid: int) -> tuple[datetime, datetime]:
    """Emit the deploy restart in app.log; returns the (down_from, up_at) gap."""
    port = sim.world.port
    t = restart_at - timedelta(seconds=2.6)
    down_from = t
    seq = [
        (t, "INFO", "uvicorn.error", "Shutting down"),
        (t + timedelta(milliseconds=103), "INFO", "uvicorn.error", "Waiting for application shutdown."),
        (t + timedelta(milliseconds=104), "INFO", "uvicorn.error", "Application shutdown complete."),
        (t + timedelta(milliseconds=105), "INFO", "uvicorn.error", f"Finished server process [{old_pid}]"),
        (restart_at, "INFO", f"{sim.pkg}.serve", f"starting {sim.world.naming.service} {version} (commit {commit_sha[:7]}) pid={new_pid}"),
        (restart_at + timedelta(milliseconds=1), "INFO", f"{sim.pkg}.config", f"loaded configuration from .env ({n_keys} keys)"),
    ]
    for w in warnings:
        seq.append((restart_at + timedelta(milliseconds=1), "WARNING", f"{sim.pkg}.config", w))
    seq += [
        (restart_at + timedelta(milliseconds=2), "INFO", f"{sim.pkg}.config", "environment=production log_level=INFO"),
        (restart_at + timedelta(milliseconds=21), "INFO", "uvicorn.error", f"Started server process [{new_pid}]"),
        (restart_at + timedelta(milliseconds=22), "INFO", "uvicorn.error", "Waiting for application startup."),
        (restart_at + timedelta(milliseconds=22), "INFO", "uvicorn.error", "Application startup complete."),
        (restart_at + timedelta(milliseconds=23), "INFO", "uvicorn.error", f"Uvicorn running on http://127.0.0.1:{port} (Press CTRL+C to quit)"),
    ]
    for ts, level, logger, msg in seq:
        sim.events.append(_Event(ts, [_fmt(ts, level, logger, msg)], sim.next_seq()))
    return down_from, restart_at + timedelta(milliseconds=30)


def _crash_loop_sequence(sim: _Sim, incident: IncidentProfile, version: str, old_pid: int, rng: random.Random) -> None:
    """Shutdown of the old process, then START_LIMIT crash attempts: a formatted 'starting' line followed by the
    raw (untimestamped) crash output, exactly as an uncaught startup exception lands in the log file."""
    t = incident.restart_at - timedelta(seconds=2.6)
    seq = [
        (t, "INFO", "uvicorn.error", "Shutting down"),
        (t + timedelta(milliseconds=103), "INFO", "uvicorn.error", "Waiting for application shutdown."),
        (t + timedelta(milliseconds=104), "INFO", "uvicorn.error", "Application shutdown complete."),
        (t + timedelta(milliseconds=105), "INFO", "uvicorn.error", f"Finished server process [{old_pid}]"),
    ]
    for ts, level, logger, msg in seq:
        sim.events.append(_Event(ts, [_fmt(ts, level, logger, msg)], sim.next_seq()))
    crash_raw = incident.extra.get("crash_output", "").rstrip("\n")
    at = incident.restart_at
    for attempt in range(5):
        pid = rng.randint(30001, 60000)
        lines = [_fmt(at, "INFO", f"{sim.pkg}.serve", f"starting {sim.world.naming.service} {version} (commit {incident.deploy_commit[:7]}) pid={pid}")]
        if crash_raw:
            lines.extend(crash_raw.splitlines())
        sim.events.append(_Event(at, lines, sim.next_seq()))
        at += timedelta(seconds=2 + rng.uniform(0.1, 0.5))


def _poisson(rng: random.Random, lam: float) -> int:
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def generate_history(world: World, incident: IncidentProfile | None, seed: int | None = None) -> dict[str, Any]:
    """Write all historical artifacts for the world; returns summary stats (also stored in world.extra)."""
    seed = world.seed if seed is None else seed
    rng = random.Random((seed * 7919) ^ 0x106)
    user_ids, products, max_order, ledger_count, ledger_last, s_count, s_last = _load_refs(world)
    sim = _Sim(rng=rng, world=world, incident=incident, user_ids=user_ids, products=products,
               max_order_id=max_order, tb=_traceback_templates(world.repo, world.naming.package),
               ledger_base_count=ledger_count, ledger_base_last=ledger_last,
               settlement_base_count=s_count, settlement_base_last=s_last)
    start, end = world.history_start, world.now
    version = re.search(r'__version__ = "([^"]+)"', (world.repo / world.naming.package / "__init__.py").read_text()).group(1)
    n_keys = len(util.parse_env_file(world.env_file.read_text()))
    base_rps = rng.uniform(1.2, 2.1)

    # red herring: a short burst of slow core-db transactions well before the incident
    herring_end_limit = incident.incident_at if incident else end
    span = (herring_end_limit - start).total_seconds()
    slow_start = start + timedelta(seconds=rng.uniform(0.2, 0.55) * span) if span > 1800 else None
    slow_end = slow_start + timedelta(minutes=rng.uniform(2, 4)) if slow_start else None

    gap: tuple[datetime, datetime] | None = None
    if incident and incident.extra.get("service_dead"):
        # the restart after the deploy crash-loops and never comes back: outage from restart to now
        old_pid = rng.randint(2000, 30000)
        _crash_loop_sequence(sim, incident, version, old_pid, rng)
        gap = (incident.restart_at - timedelta(seconds=2.6), end)
    elif incident and not incident.extra.get("no_restart"):
        old_pid, new_pid = rng.randint(2000, 30000), rng.randint(30001, 60000)
        gap = _restart_sequence(sim, incident.restart_at, incident.deploy_commit, version, n_keys,
                                incident.config_warnings, old_pid, new_pid)

    # ------------------------------------------------------------------ request stream
    minute = start.replace(second=0, microsecond=0)
    while minute < end:
        hour = minute.hour + minute.minute / 60
        n = _poisson(rng, base_rps * 60 * tp.diurnal_factor(hour))
        offsets = sorted(rng.uniform(0, 60) for _ in range(n))
        for off in offsets:
            ts = minute + timedelta(seconds=off)
            if ts < start or ts >= end:
                continue
            if gap and gap[0] <= ts <= gap[1]:
                # upstream down during the restart: nginx sees connection refused
                method, template = tp.pick_endpoint(rng)
                path = world.naming.route(template.replace("{order_id}", str(rng.randint(1, sim.max_order_id))).replace("{user_id}", str(rng.choice(user_ids))))
                ip = tp.fake_client_ip(rng)
                sim.nginx_access.append((ts, f'{ip} - - [{ts.strftime("%d/%b/%Y:%H:%M:%S +0000")}] "{method} {path} HTTP/1.1" 502 157 "-" "{rng.choice(tp.USER_AGENTS)}"'))
                sim.nginx_error.append((ts, (
                    f'{ts.strftime("%Y/%m/%d %H:%M:%S")} [error] {rng.randint(1000, 4000)}#{rng.randint(1000, 4000)}: *{rng.randint(10000, 99999)} '
                    f'connect() failed (111: Connection refused) while connecting to upstream, client: {ip}, server: {world.naming.package}.{world.domain}, '
                    f'request: "{method} {path} HTTP/1.1", upstream: "http://127.0.0.1:{world.port}{path}", host: "{world.naming.package}.{world.domain}"')))
                continue
            slow_window = bool(slow_start) and slow_start <= ts < slow_end
            _simulate_request(sim, ts, slow_window)
        minute += timedelta(minutes=1)

    # ------------------------------------------------------------------ red-herring traffic (e.g. a bot scan)
    for burst_cfg in world.extra.get("extra_traffic", []):
        if burst_cfg.get("kind") != "bot_scan":
            continue
        t = util.parse_iso(burst_cfg["start"])
        stop = min(end, t + timedelta(seconds=burst_cfg["duration_s"]))
        ip, ua = burst_cfg["ip"], burst_cfg["ua"]
        while t < stop:
            order_id = rng.randint(sim.max_order_id + 5000, sim.max_order_id + 99999)
            if not (gap and gap[0] <= t <= gap[1]):
                _record_request(sim, t, "GET", "/orders/{order_id}", f"/orders/{order_id}", 404,
                                tp.latency_ms(rng, "/orders/{order_id}"), {"order": order_id}, None, None, ua=ua, ip=ip)
            t += timedelta(seconds=rng.expovariate(burst_cfg["rps"]))

    # ------------------------------------------------------------------ probes: LB health + prometheus
    t = start.replace(second=start.second - start.second % tp.HEALTH_INTERVAL_S, microsecond=0) + timedelta(milliseconds=rng.randint(0, 900))
    while t < end:
        if not (gap and gap[0] <= t <= gap[1]):
            degraded = bool(incident) and incident.health_degraded and t >= incident.incident_at
            status = 503 if degraded else 200
            _record_request(sim, t, "GET", "/health", "/health", status, tp.latency_ms(rng, "/health"), {}, None, None,
                            ua="ELB-HealthChecker/2.0")
            if degraded:
                _bump_metric(sim, t, "db_errors_total", {"db": incident.broken_db}, 1)
        t += timedelta(seconds=tp.HEALTH_INTERVAL_S)
    t = start.replace(second=start.second - start.second % tp.METRICS_INTERVAL_S, microsecond=0) + timedelta(milliseconds=rng.randint(0, 900))
    while t < end:
        if not (gap and gap[0] <= t <= gap[1]):
            _record_request(sim, t, "GET", "/metrics", "/metrics", 200, tp.latency_ms(rng, "/metrics"), {}, None, None,
                            ua="Prometheus/2.51.0")
        t += timedelta(seconds=tp.METRICS_INTERVAL_S)
    # 'up' gauge + ledger exporter gauges per minute (exporter reads the canonical ledger file)
    import bisect

    ledger_times = sorted(sim.ledger_payment_times)
    settle_times = sorted(sim.settlement_times)
    minute = start.replace(second=0, microsecond=0)
    while minute < end:
        minute_end = minute + timedelta(minutes=1)
        if gap and gap[0] <= minute and min(minute_end, end) <= gap[1]:
            up = 0.0
        elif gap and minute <= gap[0] < minute_end:
            up = 0.75
        else:
            up = 1.0
        _bump_metric(sim, minute, "up", {}, up)
        minute_end = minute + timedelta(minutes=1)
        n = bisect.bisect_left(ledger_times, minute_end)
        last = ledger_times[n - 1] if n else sim.ledger_base_last
        _bump_metric(sim, minute, "ledger_payments_total", {}, sim.ledger_base_count + n)
        _bump_metric(sim, minute, "ledger_last_payment_age_seconds", {}, round((minute_end - last).total_seconds(), 1) if last else 0.0)
        ns = bisect.bisect_left(settle_times, minute_end)
        s_last_at = settle_times[ns - 1] if ns else sim.settlement_base_last
        _bump_metric(sim, minute, "ledger_settlements_total", {}, sim.settlement_base_count + ns)
        _bump_metric(sim, minute, "ledger_last_settlement_age_seconds",
                     {}, round((minute_end - s_last_at).total_seconds(), 1) if s_last_at else 0.0)
        minute += timedelta(minutes=1)

    # ------------------------------------------------------------------ write artifacts
    sim.events.sort(key=lambda e: (e.ts, e.seq))
    events = sim.events
    dark_since = incident.extra.get("app_log_dark_since") if incident else None
    if dark_since:  # the restarted process lost LOG_PATH and logs to stderr: app.log just stops
        dark_ts = util.parse_iso(dark_since)
        events = [e for e in events if e.ts < dark_ts]
    world.log_dir.mkdir(parents=True, exist_ok=True)
    with open(world.app_log, "w") as f:
        for ev in events:
            f.write("\n".join(ev.lines) + "\n")
    nginx_dir = world.root / "var" / "log" / "nginx"
    nginx_dir.mkdir(parents=True, exist_ok=True)
    sim.nginx_access.sort(key=lambda x: x[0])
    (nginx_dir / "access.log").write_text("".join(line + "\n" for _, line in sim.nginx_access))
    sim.nginx_error.sort(key=lambda x: x[0])
    (nginx_dir / "error.log").write_text("".join(line + "\n" for _, line in sim.nginx_error))
    _write_deploy_log(world, incident, rng)
    _write_cron_log(world, incident, rng, start, end, orders_range=(max_order, sim.max_order_id))
    _write_fleetd_log(world, incident, rng, start, end)
    _write_metrics(world, sim)
    _insert_orders(world, sim)

    world.max_order_id = sim.max_order_id
    stats = dict(sim.stats)
    stats["diverted_payments"] = len(sim.diverted_payments)
    stats["incident_error_rate"] = (stats["incident_errors"] / stats["incident_requests"]) if stats["incident_requests"] else 0.0
    stats["app_log_lines"] = util.count_lines(world.app_log)
    world.extra["history"] = stats
    world.save()
    return stats


def _write_deploy_log(world: World, incident: IncidentProfile | None, rng: random.Random) -> None:
    lines: list[str] = []
    custom = incident.extra.get("deploys") if (incident and "deploys" in incident.extra) else None
    if custom is not None:
        base_commits = world.commits[: int(incident.extra.get("n_base_commits", len(world.commits) - len(custom)))]
    else:
        base_commits = world.commits if incident is None else world.commits[:-1]
    for c in base_commits:
        when = util.parse_iso(c["when"]) + timedelta(minutes=rng.uniform(2, 7))
        sha = c["sha"][:7]
        lines += _deploy_lines(world.naming.service, when, sha, c["author"], c["message"], config_only=c["message"].startswith(("ops:", "chore: rotate")), rng=rng)
    herring = world.extra.get("herring_deploys", [])
    if herring:
        custom = sorted((custom or []) + herring, key=lambda d: d["when"])
    if custom:
        for d in custom:
            restart_at = incident.restart_at if d.get("restart") == "restart" else None
            lines += _deploy_lines(world.naming.service, util.parse_iso(d["when"]), d["sha"], d["author"], d["message"], config_only=bool(d.get("config_only")),
                                   rng=rng, restart_at=restart_at, restart=d.get("restart", "restart"),
                                   deps_line=d.get("deps_line"), crashed=bool(d.get("crashed")), ship_note=d.get("ship_note"))
    elif incident and custom is None:
        lines += _deploy_lines(world.naming.service, incident.deploy_at, incident.deploy_commit[:7], incident.deploy_author, incident.deploy_message,
                               config_only=True, rng=rng, restart_at=incident.restart_at)
    (world.log_dir / "deploy.log").write_text("".join(l + "\n" for l in lines))


def _deploy_lines(svc: str, when: datetime, sha: str, author: str, message: str, config_only: bool, rng: random.Random,
                  restart_at: datetime | None = None, restart: str = "restart", deps_line: str | None = None,
                  crashed: bool = False, ship_note: str | None = None) -> list[str]:
    tag = f"[{svc}]"
    t = when
    out = [f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} deploy {sha} requested by {author} ({message})"]
    t += timedelta(seconds=rng.uniform(1, 3))
    out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} git fetch origin && git checkout {sha} ... ok")
    if config_only:
        t += timedelta(seconds=rng.uniform(1, 2))
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} shipping .env (1 file changed)" + (f" ... {ship_note}" if ship_note else ""))
    else:
        t += timedelta(seconds=rng.uniform(4, 12))
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} deps: python scripts/deploy_deps.py ... {deps_line or 'reqlog==2.1.0 installed (no change)'}")
        t += timedelta(seconds=rng.uniform(0.5, 1.5))
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} db migrations: not run by deploy-bot (manual step, see runbook)")
    if restart == "deferred":
        t += timedelta(seconds=rng.uniform(1, 2))
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} config-only change: service restart deferred (takes effect on next restart)")
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} deploy complete in {int((t - when).total_seconds())}s")
        return out
    t = restart_at - timedelta(seconds=2.7) if restart_at else t + timedelta(seconds=rng.uniform(1, 2))
    out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} restarting service (systemctl restart {svc})")
    if crashed:
        t = (restart_at or t) + timedelta(seconds=rng.uniform(12, 16))
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} ERROR: service did not become active within 10s "
                   f"(systemctl reports: activating (auto-restart) -> failed, Result: start-limit-hit)")
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} deploy FAILED after {int((t - when).total_seconds())}s; "
                   f"manual intervention required (deploy-bot does not auto-roll-back)")
        return out
    t = restart_at + timedelta(seconds=rng.uniform(1.5, 3)) if restart_at else t + timedelta(seconds=rng.uniform(2, 4))
    out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} service active (pid {rng.randint(2000, 60000)})")
    out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} deploy complete in {int((t - when).total_seconds())}s")
    return out


def _write_cron_log(world: World, incident: IncidentProfile | None, rng: random.Random, start: datetime, end: datetime,
                    orders_range: tuple[int, int] = (0, 0)) -> None:
    entries: list[tuple[datetime, str]] = []
    ttl = util.parse_env_file(world.env_file.read_text()).get("CART_TTL_MINUTES", "45")
    core_unreachable = bool(incident) and incident.broken_db == "core" and not incident.extra.get("lock_burst") and incident.failing_endpoints
    t = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0)
    while t < end:
        if t >= start:
            at = t + timedelta(seconds=rng.uniform(0.2, 1.8))
            if core_unreachable and t >= incident.incident_at and "unable to open" in (incident.error_message or ""):
                entries.append((at, f"{at:%Y-%m-%d %H:%M:%S} expire_carts: ERROR OperationalError: unable to open database file"))
            else:
                scanned = rng.randint(3, 14)
                entries.append((at, f"{at:%Y-%m-%d %H:%M:%S} expire_carts: scanned {scanned} active carts, expired {rng.randint(0, min(3, scanned))} (ttl={ttl}m)"))
        t += timedelta(minutes=15)
    burst = incident.extra.get("lock_burst") if incident else None
    if burst:
        reload_at = (util.parse_iso(burst["since"]) if burst.get("since") else incident.incident_at) - timedelta(seconds=rng.uniform(20, 70))
        entries.append((reload_at, f"{reload_at:%Y-%m-%d %H:%M:%S} crond[{rng.randint(300, 900)}]: (*system*{world.naming.service}) RELOAD (/etc/cron.d/{world.naming.service})"))
        burst_since = util.parse_iso(burst["since"]) if burst.get("since") else incident.incident_at
        t = burst_since.replace(second=0, microsecond=0)
        n0, n1 = orders_range
        span_s = max(1.0, (end - start).total_seconds())
        while t < end:
            if t >= burst_since - timedelta(seconds=5):
                done = t + timedelta(seconds=burst["offset_s"] + burst["duration_s"] + rng.uniform(0.1, 0.6))
                if done < end:
                    n_at = int(n0 + (n1 - n0) * min(1.0, (t - start).total_seconds() / span_s))
                    held = burst["duration_s"] * (0.9 + 0.2 * (n_at / max(1, n1))) + rng.uniform(-0.6, 0.6)
                    entries.append((done, burst["cron_line"].format(stamp=f"{done:%Y-%m-%d %H:%M:%S}", n=n_at + rng.randint(-4, 4),
                                                                    checksum="%08x" % rng.getrandbits(32), held=f"{held:.1f}")))
            t += timedelta(seconds=burst["period_s"])
    entries.sort(key=lambda e: e[0])
    (world.log_dir / "cron.log").write_text("".join(l + "\n" for _, l in entries))


def _write_fleetd_log(world: World, incident: IncidentProfile | None, rng: random.Random, start: datetime, end: datetime) -> None:
    """Host configuration-management agent ("fleetd") log: routine policy syncs, plus any events a fault
    template staged in ``incident.extra['fleetd_events']`` ([(iso_ts, text), ...])."""
    pid = rng.randint(300, 900)
    entries: list[tuple[datetime, str]] = []
    t = start + timedelta(minutes=rng.uniform(2, 20))
    while t < end:
        entries.append((t, f"{t:%Y-%m-%d %H:%M:%S} fleetd[{pid}]: policy sync completed: 0 changes "
                           f"(perms-baseline-v3, pkg-inventory-v9) in {rng.uniform(0.4, 2.2):.1f}s"))
        t += timedelta(minutes=rng.uniform(17, 43))
    if incident:
        for iso, text in incident.extra.get("fleetd_events", []):
            at = util.parse_iso(iso)
            entries.append((at, f"{at:%Y-%m-%d %H:%M:%S} fleetd[{pid}]: {text}"))
    entries.sort(key=lambda e: e[0])
    path = world.root / "var" / "log" / "fleetd.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(l + "\n" for _, l in entries))


def _write_metrics(world: World, sim: _Sim) -> None:
    world.metrics_file.parent.mkdir(parents=True, exist_ok=True)
    with open(world.metrics_file, "w") as f:
        for minute in sorted(sim.metrics):
            for key, value in sorted(sim.metrics[minute].items()):
                name, labels_json = key.split("|", 1)
                f.write(json.dumps({"ts": minute, "m": name, "l": json.loads(labels_json), "v": round(value, 3)}) + "\n")


def _insert_orders(world: World, sim: _Sim) -> None:
    if not sim.new_orders:
        return
    core = sqlite3.connect(world.core_db)
    core.executemany("INSERT INTO orders (id, user_id, status, total_cents, currency, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", sim.new_orders)
    core.executemany("INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents) VALUES (?, ?, ?, ?)", sim.new_items)
    core.commit()
    core.close()
    ledger = sqlite3.connect(world.ledger_db)
    ledger.executemany("INSERT INTO payments (order_id, user_id, amount_cents, currency, method, status, gateway_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", sim.new_payments)
    accepted_refs = {p[6] for p in sim.new_payments}
    try:
        ledger.executemany("INSERT OR IGNORE INTO settlements (gateway_ref, order_id, amount_cents, settled_at) VALUES (?, ?, ?, ?)",
                           [row for row in sim.new_settlements if row[0] in accepted_refs])
    except sqlite3.OperationalError:
        pass  # ledger predates the settlements table
    ledger.commit()
    ledger.close()
    if sim.diverted_payments and sim.incident and sim.incident.extra.get("payments_db"):
        diverted_refs = {p[6] for p in sim.diverted_payments}
        other = sqlite3.connect(world.repo / sim.incident.extra["payments_db"])
        other.executemany("INSERT INTO payments (order_id, user_id, amount_cents, currency, method, status, gateway_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", sim.diverted_payments)
        try:
            other.executemany("INSERT OR IGNORE INTO settlements (gateway_ref, order_id, amount_cents, settled_at) VALUES (?, ?, ?, ?)",
                              [row for row in sim.new_settlements if row[0] in diverted_refs])
        except sqlite3.OperationalError:
            pass  # snapshot predates the settlements table
        other.commit()
        other.close()
