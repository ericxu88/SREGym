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
from sregym.generator.world import SERVICE_NAME, World

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
    diverted_payments: list[tuple] = field(default_factory=list)  # -> incident.extra["payments_db"] (e.g. a stale snapshot)
    ledger_payment_times: list[datetime] = field(default_factory=list)  # payments that reached the real ledger
    ledger_base_count: int = 0
    ledger_base_last: datetime | None = None
    metrics: dict[str, dict] = field(default_factory=dict)  # minute-iso -> aggregates
    seq: int = 0
    stats: dict[str, Any] = field(default_factory=lambda: {"requests": 0, "errors": 0, "incident_requests": 0, "incident_errors": 0})

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


def _fmt(ts: datetime, level: str, logger: str, msg: str) -> str:
    return f"{util.fmt_log_ts(ts)} {level:<7} {logger} {msg}"


def _find_line(lines: list[str], needle: str, start: int = 0) -> int:
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i + 1
    raise ValueError(f"line containing {needle!r} not found")


def _traceback_templates(repo: Path) -> dict[str, list[str]]:
    """Traceback text per failing handler, with line numbers taken from the real source."""
    main = (repo / "checkout" / "main.py").read_text().splitlines()
    db = (repo / "checkout" / "db.py").read_text().splitlines()
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
            f'  File "checkout/main.py", line {l_call_next}, in observe_requests',
            "    response = await call_next(request)",
            f'  File "checkout/main.py", line {handler_with(handler, needle)}, in {handler}',
            f"    {needle}",
            f'  File "checkout/db.py", line {l_db}, in {db_func}',
            f"    {db_src}",
            f'  File "checkout/db.py", line {l_session}, in _session',
            "    conn = _connect(url)",
            f'  File "checkout/db.py", line {l_connect}, in _connect',
            f"    {connect_src}",
        ]

    out = {}
    for handler in ("get_order", "list_orders", "get_user", "list_users", "checkout"):
        out[handler] = build(handler, "with core_db() as conn:", "core_db", l_core, core_src)
    out["checkout_ledger"] = build("checkout", "with ledger_db() as ledger:", "ledger_db", l_ledger, ledger_src)
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
    finally:
        ledger.close()
    return user_ids, products, max_order, int(count), (util.parse_iso(last) if last else None)


def _bump_metric(sim: _Sim, ts: datetime, name: str, labels: dict[str, str], value: float) -> None:
    minute = ts.replace(second=0, microsecond=0)
    bucket = sim.metrics.setdefault(util.fmt_iso(minute), {})
    key = name + "|" + json.dumps(labels, sort_keys=True)
    bucket[key] = bucket.get(key, 0) + value


def _record_request(sim: _Sim, ts: datetime, method: str, template: str, path: str, status: int,
                    latency: float, extra: dict[str, Any], error: str | None, tb_key: str | None,
                    warn_lines: list[str] | None = None, ua: str | None = None) -> None:
    rng = sim.rng
    req_id = "%08x" % rng.getrandbits(32)
    lines: list[str] = []
    for w in warn_lines or []:
        lines.append(_fmt(ts, "WARNING", "checkout.db", w))
    if tb_key:
        for tb_line in sim.tb[tb_key] + [error]:
            lines.append(_fmt(ts, "ERROR", "checkout.app", f"req={req_id} {tb_line}"))
    msg = f"req={req_id} {method} {path} {status} {latency:.0f}ms"
    if extra:
        msg += " " + " ".join(f"{k}={v}" for k, v in extra.items())
    if error:
        msg += f' error="{error}"'
    level = "ERROR" if status >= 500 else "INFO"
    lines.append(_fmt(ts, level, "checkout.access", msg))
    sim.events.append(_Event(ts, lines, sim.next_seq()))
    # metrics
    _bump_metric(sim, ts, "http_requests_total", {"method": method, "path": template, "status": str(status)}, 1)
    _bump_metric(sim, ts, "http_request_duration_ms_sum", {"path": template}, round(latency, 3))
    _bump_metric(sim, ts, "http_request_duration_ms_count", {"path": template}, 1)
    if error and sim.incident:
        _bump_metric(sim, ts, "db_errors_total", {"db": sim.incident.broken_db}, 1)
    # nginx access log (health probes are access_log off in the nginx config)
    if template != "/health":
        ua = ua or rng.choice(tp.USER_AGENTS)
        size = {200: rng.randint(180, 620), 201: rng.randint(150, 220), 400: rng.randint(40, 90), 404: 27,
                422: rng.randint(90, 160), 429: 32, 500: 98, 503: rng.randint(150, 220)}.get(status, 100)
        ip = "10.0.4.12" if template == "/metrics" else tp.fake_client_ip(rng)
        sim.nginx_access.append((ts, f'{ip} - - [{ts.strftime("%d/%b/%Y:%H:%M:%S +0000")}] "{method} {path} HTTP/1.1" {status} {size} "-" "{ua}"'))
    sim.stats["requests"] += 1
    if status >= 500:
        sim.stats["errors"] += 1
    if sim.incident and ts >= sim.incident.incident_at:
        sim.stats["incident_requests"] += 1
        if status >= 500:
            sim.stats["incident_errors"] += 1


def _simulate_request(sim: _Sim, ts: datetime, slow_window: bool) -> None:
    rng, inc = sim.rng, sim.incident
    method, template = tp.pick_endpoint(rng)
    key = f"{method} {template}"
    failing = bool(inc) and ts >= inc.incident_at and key in inc.failing_endpoints
    core_broken = bool(inc) and inc.broken_db == "core"
    slow = slow_window and template != "/checkout" and rng.random() < 0.4
    latency = tp.latency_ms(rng, template, slow=slow)
    warn = [f"slow database transaction ({latency:.0f}ms) db=core"] if slow else None
    extra: dict[str, Any] = {}
    error = tb_key = None
    status = 200

    if template == "/checkout":
        user_id = rng.choice(sim.user_ids)
        extra["user"] = user_id
        path = "/checkout"
        r = rng.random()
        if failing:
            status, error = 500, inc.error_message
            tb_key = "checkout" if core_broken else "checkout_ledger"
            latency = tp.latency_ms(rng, "/orders")  # fails fast
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
            for p in items:
                qty = rng.choice([1, 1, 1, 2, 3])
                total += p["price_cents"] * qty
                sim.new_items.append((order_id, p["id"], qty, p["price_cents"]))
            sim.new_orders.append((order_id, user_id, "confirmed", total, "USD", created, created))
            payment = (order_id, user_id, total, "USD", rng.choice(["card"] * 7 + ["paypal"] * 2 + ["apple_pay"]),
                       "captured", "ch_%016x" % rng.getrandbits(64), created)
            diverted = bool(inc) and ts >= inc.incident_at and bool(inc.extra.get("payments_db"))
            if diverted:
                sim.diverted_payments.append(payment)
            else:
                sim.new_payments.append(payment)
                sim.ledger_payment_times.append(ts)
    elif template == "/orders/{order_id}":
        if rng.random() < 0.05:
            order_id = rng.randint(sim.max_order_id + 1, sim.max_order_id + 20000)
            status = 404
        else:
            order_id = rng.randint(max(1, sim.max_order_id - 4000), sim.max_order_id)
        extra["order"] = order_id
        path = f"/orders/{order_id}"
        if failing:
            status, error, tb_key = 500, inc.error_message, "get_order"
    elif template == "/orders":
        user_id = rng.choice(sim.user_ids)
        extra["user"] = user_id
        path = f"/orders?user_id={user_id}" + rng.choice(["", "", "&limit=10", "&status=confirmed", "&limit=50&offset=0"])
        if failing:
            status, error, tb_key = 500, inc.error_message, "list_orders"
    elif template == "/users/{user_id}":
        if rng.random() < 0.02:
            user_id = rng.randint(sim.user_ids[-1] + 1, sim.user_ids[-1] + 5000)
            status = 404
        else:
            user_id = rng.choice(sim.user_ids)
        extra["user"] = user_id
        path = f"/users/{user_id}"
        if failing:
            status, error, tb_key = 500, inc.error_message, "get_user"
    else:  # /users
        path = "/users?" + rng.choice(["limit=20", "limit=50", "limit=20&offset=20", "limit=100&offset=100"])
        if failing:
            status, error, tb_key = 500, inc.error_message, "list_users"
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
        (restart_at, "INFO", "checkout.serve", f"starting {SERVICE_NAME} {version} (commit {commit_sha[:7]}) pid={new_pid}"),
        (restart_at + timedelta(milliseconds=1), "INFO", "checkout.config", f"loaded configuration from .env ({n_keys} keys)"),
    ]
    for w in warnings:
        seq.append((restart_at + timedelta(milliseconds=1), "WARNING", "checkout.config", w))
    seq += [
        (restart_at + timedelta(milliseconds=2), "INFO", "checkout.config", "environment=production log_level=INFO"),
        (restart_at + timedelta(milliseconds=21), "INFO", "uvicorn.error", f"Started server process [{new_pid}]"),
        (restart_at + timedelta(milliseconds=22), "INFO", "uvicorn.error", "Waiting for application startup."),
        (restart_at + timedelta(milliseconds=22), "INFO", "uvicorn.error", "Application startup complete."),
        (restart_at + timedelta(milliseconds=23), "INFO", "uvicorn.error", f"Uvicorn running on http://127.0.0.1:{port} (Press CTRL+C to quit)"),
    ]
    for ts, level, logger, msg in seq:
        sim.events.append(_Event(ts, [_fmt(ts, level, logger, msg)], sim.next_seq()))
    return down_from, restart_at + timedelta(milliseconds=30)


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
    user_ids, products, max_order, ledger_count, ledger_last = _load_refs(world)
    sim = _Sim(rng=rng, world=world, incident=incident, user_ids=user_ids, products=products,
               max_order_id=max_order, tb=_traceback_templates(world.repo),
               ledger_base_count=ledger_count, ledger_base_last=ledger_last)
    start, end = world.history_start, world.now
    version = re.search(r'__version__ = "([^"]+)"', (world.repo / "checkout" / "__init__.py").read_text()).group(1)
    n_keys = len(util.parse_env_file(world.env_file.read_text()))
    base_rps = rng.uniform(1.2, 2.1)

    # red herring: a short burst of slow core-db transactions well before the incident
    herring_end_limit = incident.incident_at if incident else end
    span = (herring_end_limit - start).total_seconds()
    slow_start = start + timedelta(seconds=rng.uniform(0.2, 0.55) * span) if span > 1800 else None
    slow_end = slow_start + timedelta(minutes=rng.uniform(2, 4)) if slow_start else None

    gap: tuple[datetime, datetime] | None = None
    if incident:
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
                path = template.replace("{order_id}", str(rng.randint(1, sim.max_order_id))).replace("{user_id}", str(rng.choice(user_ids)))
                ip = tp.fake_client_ip(rng)
                sim.nginx_access.append((ts, f'{ip} - - [{ts.strftime("%d/%b/%Y:%H:%M:%S +0000")}] "{method} {path} HTTP/1.1" 502 157 "-" "{rng.choice(tp.USER_AGENTS)}"'))
                sim.nginx_error.append((ts, (
                    f'{ts.strftime("%Y/%m/%d %H:%M:%S")} [error] {rng.randint(1000, 4000)}#{rng.randint(1000, 4000)}: *{rng.randint(10000, 99999)} '
                    f'connect() failed (111: Connection refused) while connecting to upstream, client: {ip}, server: checkout.{world.domain}, '
                    f'request: "{method} {path} HTTP/1.1", upstream: "http://127.0.0.1:{world.port}{path}", host: "checkout.{world.domain}"')))
                continue
            slow_window = bool(slow_start) and slow_start <= ts < slow_end
            _simulate_request(sim, ts, slow_window)
        minute += timedelta(minutes=1)

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
    minute = start.replace(second=0, microsecond=0)
    while minute < end:
        up = 1.0
        if gap and minute <= gap[0] < minute + timedelta(minutes=1):
            up = 0.75
        _bump_metric(sim, minute, "up", {}, up)
        minute_end = minute + timedelta(minutes=1)
        n = bisect.bisect_left(ledger_times, minute_end)
        last = ledger_times[n - 1] if n else sim.ledger_base_last
        _bump_metric(sim, minute, "ledger_payments_total", {}, sim.ledger_base_count + n)
        _bump_metric(sim, minute, "ledger_last_payment_age_seconds", {}, round((minute_end - last).total_seconds(), 1) if last else 0.0)
        minute += timedelta(minutes=1)

    # ------------------------------------------------------------------ write artifacts
    sim.events.sort(key=lambda e: (e.ts, e.seq))
    world.log_dir.mkdir(parents=True, exist_ok=True)
    with open(world.app_log, "w") as f:
        for ev in sim.events:
            f.write("\n".join(ev.lines) + "\n")
    nginx_dir = world.root / "var" / "log" / "nginx"
    nginx_dir.mkdir(parents=True, exist_ok=True)
    sim.nginx_access.sort(key=lambda x: x[0])
    (nginx_dir / "access.log").write_text("".join(line + "\n" for _, line in sim.nginx_access))
    sim.nginx_error.sort(key=lambda x: x[0])
    (nginx_dir / "error.log").write_text("".join(line + "\n" for _, line in sim.nginx_error))
    _write_deploy_log(world, incident, rng)
    _write_cron_log(world, incident, rng, start, end)
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
    custom = incident.extra.get("deploys") if incident else None
    if custom:
        base_commits = world.commits[: int(incident.extra.get("n_base_commits", len(world.commits) - len(custom)))]
    else:
        base_commits = world.commits if incident is None else world.commits[:-1]
    for c in base_commits:
        when = util.parse_iso(c["when"]) + timedelta(minutes=rng.uniform(2, 7))
        sha = c["sha"][:7]
        lines += _deploy_lines(when, sha, c["author"], c["message"], config_only=c["message"].startswith(("ops:", "chore: rotate")), rng=rng)
    if custom:
        for d in custom:
            restart_at = incident.restart_at if d.get("restart") == "restart" else None
            lines += _deploy_lines(util.parse_iso(d["when"]), d["sha"], d["author"], d["message"], config_only=bool(d.get("config_only")),
                                   rng=rng, restart_at=restart_at, restart=d.get("restart", "restart"))
    elif incident:
        lines += _deploy_lines(incident.deploy_at, incident.deploy_commit[:7], incident.deploy_author, incident.deploy_message,
                               config_only=True, rng=rng, restart_at=incident.restart_at)
    (world.log_dir / "deploy.log").write_text("".join(l + "\n" for l in lines))


def _deploy_lines(when: datetime, sha: str, author: str, message: str, config_only: bool, rng: random.Random,
                  restart_at: datetime | None = None, restart: str = "restart") -> list[str]:
    tag = f"[{SERVICE_NAME}]"
    t = when
    out = [f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} deploy {sha} requested by {author} ({message})"]
    t += timedelta(seconds=rng.uniform(1, 3))
    out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} git fetch origin && git checkout {sha} ... ok")
    if config_only:
        t += timedelta(seconds=rng.uniform(1, 2))
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} shipping .env (1 file changed)")
    else:
        t += timedelta(seconds=rng.uniform(4, 12))
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} pip install -r requirements.txt ... up to date")
    if restart == "deferred":
        t += timedelta(seconds=rng.uniform(1, 2))
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} config-only change: service restart deferred (takes effect on next restart)")
        out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} deploy complete in {int((t - when).total_seconds())}s")
        return out
    t = restart_at - timedelta(seconds=2.7) if restart_at else t + timedelta(seconds=rng.uniform(1, 2))
    out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} restarting service (systemctl restart {SERVICE_NAME})")
    t = restart_at + timedelta(seconds=rng.uniform(1.5, 3)) if restart_at else t + timedelta(seconds=rng.uniform(2, 4))
    out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} service active (pid {rng.randint(2000, 60000)})")
    out.append(f"{t:%Y-%m-%d %H:%M:%S} deploy-bot: {tag} deploy complete in {int((t - when).total_seconds())}s")
    return out


def _write_cron_log(world: World, incident: IncidentProfile | None, rng: random.Random, start: datetime, end: datetime) -> None:
    lines: list[str] = []
    ttl = util.parse_env_file(world.env_file.read_text()).get("CART_TTL_MINUTES", "45")
    t = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0)
    while t < end:
        if t >= start:
            stamp = (t + timedelta(seconds=rng.uniform(0.2, 1.8))).strftime("%Y-%m-%d %H:%M:%S")
            if incident and incident.broken_db == "core" and t >= incident.incident_at:
                lines.append(f"{stamp} expire_carts: ERROR OperationalError: unable to open database file")
            else:
                scanned = rng.randint(3, 14)
                lines.append(f"{stamp} expire_carts: scanned {scanned} active carts, expired {rng.randint(0, min(3, scanned))} (ttl={ttl}m)")
        t += timedelta(minutes=15)
    (world.log_dir / "cron.log").write_text("".join(l + "\n" for l in lines))


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
    ledger.commit()
    ledger.close()
    if sim.diverted_payments and sim.incident and sim.incident.extra.get("payments_db"):
        other = sqlite3.connect(world.repo / sim.incident.extra["payments_db"])
        other.executemany("INSERT INTO payments (order_id, user_id, amount_cents, currency, method, status, gateway_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", sim.diverted_payments)
        other.commit()
        other.close()
