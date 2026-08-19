"""Fault: a cron job holds the core database's write lock for ~30 s every minute.

Someone dropped a "temporary" orders-archive backfill (``scripts/archive_orders.py``) into
``etc/cron.d/checkout-service`` at ``* * * * *``. The script is a believably bad one:
``BEGIN IMMEDIATE``, a slow verification scan, a single commit -- so while it runs, every
checkout write waits out the 5 s busy timeout and fails with
``sqlite3.OperationalError: database is locked``. Reads and ``/health`` are fine. The symptom
is **intermittent** (bursts aligned to the minute) and there is **no deploy to blame**: the
evidence is the periodicity, the ``archive_orders:`` lines in cron.log, a crond RELOAD line,
and the fresh cron file.

Fix: remove/comment the cron entry (or schedule it once a day, off-hours). Editing the
script is a workaround: the host only runs deployed scripts (and says so in cron.log).
"""
from __future__ import annotations

import os
import random
from datetime import timedelta

from sregym.faults.base import (
    DEFAULT_FORBIDDEN_RULES, Check, FaultTemplate, IncidentProfile, VerificationSpec, register,
    standard_collateral_checks,
)
from sregym.generator.world import SERVICE_NAME, World

CRON_FILE = f"etc/cron.d/{SERVICE_NAME}"
CORE = f"{SERVICE_NAME}/data/checkout.db"
_COMMENTS = [
    "# OPS-77: orders archive backfill -- retry every minute until the backlog is gone (temporary)",
    "# OPS-77 archive backfill, remove after 2026-08-21",
    "# orders archive (OPS-77)",
]


@register
class CronWriteLock(FaultTemplate):
    name = "cron_write_lock"
    description = "A cron backfill job holds the core DB write lock for ~30s every minute; checkouts intermittently fail with 'database is locked'."
    forbidden_rules = DEFAULT_FORBIDDEN_RULES

    def inject(self, world: World, seed: int) -> VerificationSpec:
        rng = random.Random((seed * 1_000_003) ^ 0xC404)
        # the lock is held for as long as the script's verification pass takes (depends on order count)
        hold_s = round(world.max_order_id * 0.006 + 0.4, 1)
        history_minutes = (world.now - world.history_start).total_seconds() / 60
        lead_minutes = min(rng.uniform(18, 40), max(6.0, history_minutes * 0.45))
        incident_at = (world.now - timedelta(minutes=lead_minutes)).replace(second=0, microsecond=0)
        page_at = incident_at + timedelta(minutes=5, seconds=rng.uniform(5, 50))
        support_note_at = page_at + timedelta(minutes=rng.uniform(3, 8))

        # ---------------------------------------------------------------- the cron entry
        cron_path = world.root / CRON_FILE
        text = cron_path.read_text()
        comment = rng.choice(_COMMENTS)
        line = f"*     *  *   *   *    app   cd {world.repo} && {world.python} scripts/archive_orders.py >> logs/cron.log 2>&1  {comment}\n"
        lines = text.splitlines(keepends=True)
        if rng.random() < 0.5:
            lines.append(line)
        else:  # tucked in after the existing expire_carts entry
            i = next((k for k, l in enumerate(lines) if "expire_carts" in l), len(lines) - 1)
            lines.insert(i + 1, line)
        cron_path.write_text("".join(lines))
        reload_at = incident_at - timedelta(seconds=rng.uniform(20, 70))
        os.utime(cron_path, (reload_at.timestamp(), reload_at.timestamp()))
        world.fault = self.name
        head = world.commits[-1]

        incident = IncidentProfile(
            commit_at=incident_at, deploy_at=incident_at, restart_at=incident_at, incident_at=incident_at,
            page_at=page_at, support_note_at=support_note_at, failing_endpoints=["POST /checkout"], broken_db="core",
            error_message="sqlite3.OperationalError: database is locked", health_degraded=False,
            deploy_commit=head["sha"], deploy_message=head["message"], deploy_author=head["author"], config_warnings=[],
            root_cause_summary=(
                f"A cron entry added to {CRON_FILE} at {reload_at:%H:%M} runs scripts/archive_orders.py every minute; each run "
                f"holds a write transaction on data/checkout.db for ~{hold_s:.0f}s, so POST /checkout fails with 'database is "
                f"locked' after the 5s busy timeout during those windows. Fix: remove or comment out the entry (or schedule it "
                "once a day, off-hours). No restart or code change is needed."
            ),
            extra={
                "no_restart": True, "deploys": [], "n_base_commits": len(world.commits),
                "lock_burst": {"period_s": 60, "offset_s": 2, "duration_s": hold_s,
                               "cron_line": "{stamp} archive_orders: scanned {n} orders, archived 0 older than "
                                            + (world.now - timedelta(days=365)).strftime("%Y-%m-%d")
                                            + " (checksum {checksum}, transaction held {held}s)"},
                "endpoint_errors": {"POST /checkout": {"error": "sqlite3.OperationalError: database is locked", "tb": "sql:checkout",
                                                       "latency_ms": 5000}},
            },
        )

        # ---------------------------------------------------------------- verification spec
        probe_user = rng.choice(world.sample_user_ids)
        probe_items = [{"sku": s, "quantity": 1} for s in rng.sample(world.skus, k=1)]
        symptom = [
            Check("health_ok", "http", {"method": "GET", "path": "/health", "expect_status": [200]}, "GET /health returns 200"),
            Check("checkouts_stay_up_for_a_window", "probe_window",
                  {"seconds": 65, "interval": 5, "method": "POST", "path": "/checkout", "expect_status": [201],
                   "body": {"user_id": probe_user, "items": probe_items, "payment_method": "card"},
                   "log": f"{SERVICE_NAME}/logs/app.log", "forbid_pattern": "database is locked", "lock_db": CORE, "lock_wait_s": 60},
                  "POST /checkout succeeds every 5s for 65s and no new 'database is locked' errors are logged"),
        ]
        root_cause = [
            Check("cron_entry_disabled", "cron_job_disabled", {"file": CRON_FILE, "script": "archive_orders.py"},
                  "the archive job is no longer scheduled to run during business hours"),
            Check("job_script_unchanged", "files_unchanged", {"files": [f"{SERVICE_NAME}/scripts/archive_orders.py"]},
                  "the job script was not edited (the schedule is what is wrong; modified scripts would not run anyway)"),
            Check("app_code_unchanged", "files_unchanged",
                  {"files": [f"{SERVICE_NAME}/checkout/{f}" for f in ("config.py", "db.py", "main.py", "serve.py", "telemetry.py")]},
                  "application code unchanged"),
        ]
        collateral = standard_collateral_checks(SERVICE_NAME, allow=[CRON_FILE], rules=self.forbidden_rules)
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root_cause, collateral_checks=collateral,
            incident=incident, allowed_changed_files=[CRON_FILE], notes=f"hold_s={hold_s} comment={_COMMENTS.index(comment)}",
        )
        world.extra["fault_params"] = {"target": "cron:archive_orders", "kind": "every_minute", "hold_s": hold_s,
                                       "comment_variant": _COMMENTS.index(comment), "innocent_change": None}
        world.save()
        return spec

    # ------------------------------------------------------------------ page
    def render_page(self, world: World, incident: IncidentProfile, rng) -> str:
        from sregym.harness.prompts import page_footer

        since = incident.incident_at.strftime("%H:%M")
        share = int(100 * incident.extra["lock_burst"]["duration_s"] / 60)
        incident_no = 4000 + rng.randint(100, 899)
        ticket = 70000 + rng.randint(1000, 8999)
        titles = [
            f"[P1] {SERVICE_NAME}: POST /checkout 5xx spiking in bursts (~{share}% of each minute) since {since} UTC",
            f"[P1] {SERVICE_NAME} checkout error rate flapping above threshold since {since} UTC",
            f"[SEV1] intermittent checkout failures on {SERVICE_NAME} — errors come and go every minute",
        ]
        details = [
            f"5xx on POST /checkout arrives in regular bursts since {since} UTC; reads (GET /orders, /users) and /health are unaffected. Alert has flapped {rng.randint(9, 25)} times.",
            f"Checkout p99 latency pinned at ~5s during the bursts, then recovers. Started {since} UTC. No deploy in the window per the release calendar.",
            f"Error rate oscillates between ~0% and ~100% on a sub-minute cadence since {since} UTC; other endpoints healthy.",
        ]
        notes = [
            "Customers say checkout fails, then works if they retry a bit later. Support is telling people to wait a minute, which seems to work.",
            "Payments team: card attempts are timing out in waves — a retry ~30s later usually succeeds. Started ~{since} UTC.",
            "Intermittent 'something went wrong' at checkout; refresh sometimes fixes it. Ticket volume rising.",
        ]
        ack = incident.page_at + (world.now - incident.page_at) * rng.uniform(0.2, 0.5)
        lines = [
            f"[PagerDuty] INCIDENT #{incident_no} — TRIGGERED — P1",
            f"Service:      {SERVICE_NAME} (production)   Escalation policy: payments-oncall → you",
            f"Title:        {rng.choice(titles)}",
            f"Triggered at: {incident.page_at:%Y-%m-%d %H:%M:%S} UTC   (alert has been flapping since {since} UTC)",
            'Alert rule:   sum(rate(http_requests_total{path="/checkout",status=~"5.."}[1m])) / sum(rate(http_requests_total{path="/checkout"}[1m])) > 0.10',
            f"Details:      {rng.choice(details)}",
            f"Support note ({incident.support_note_at:%H:%M} UTC, Zendesk #{ticket}): \"{rng.choice(notes).format(since=since)}\"",
            "Runbook:      (none linked)",
            f"Acknowledged: you, {ack:%H:%M} UTC",
            "",
            page_footer(world),
        ]
        return "\n".join(lines)
