"""Fault: the payments ledger was pointed at a stale on-disk copy -- silent data divergence.

A config change set ``LEDGER_DATABASE_URL`` to the weekly audit snapshot
(``data/ledger-snapshot-YYYYMMDD.db``) instead of ``data/ledger.db``. Nothing errors:
``/checkout`` keeps returning 201 and the app happily records payments ... in the snapshot.
The real ledger stops growing, which is what the finance-side ledger exporter alerts on
(``ledger_last_payment_age_seconds``).

Seeded parameters
  * how stale the snapshot is, and whether an older second snapshot also exists
  * the commit that made the change (a "staging-only" dry-run that hit prod, a mistaken
    "restore", or a copy-paste error in a tidy-up commit)
  * **causal depth**: half the time the config was shipped *hours earlier* with the restart
    deferred, and an innocent release commit later restarted the service and activated it
    (``git show HEAD`` then points at the wrong change)

A complete fix = restore the ledger URL, restart, **and** copy the payments that landed in
the snapshot back into the real ledger (``scripts/reconcile_ledger.py --source ... --apply``).
Config + restart alone stops the bleeding but leaves the ledger incomplete (reward 0.7).
"""
from __future__ import annotations

import random
import shutil
import sqlite3
from datetime import timedelta

from sregym import util
from sregym.faults.base import (
    DEFAULT_FORBIDDEN_RULES, Check, FaultTemplate, IncidentProfile, VerificationSpec, register,
    standard_collateral_checks,
)
from sregym.generator.world import LEDGER_DB, SERVICE_NAME, World
from sregym.harness.prompts import page_footer

_CONFIG_COMMITS = [
    ("fin: run the ledger against the audit snapshot for the FIN-212 reconciliation dry-run\n\n"
     "STAGING ONLY -- do not deploy to prod. Revert after the dry-run.", None),
    ("ops: switch ledger to the restored copy after the disk-pressure alert (OPS-501)", None),
    ("chore(config): normalize data paths in production .env", ("DATABASE_TIMEOUT_SECONDS", "5", "10")),
]
_RELEASE_COMMITS = [
    "chore: release {ver} (docs refresh, no functional changes)",
    "docs: clarify on-call notes and release {ver}",
]


@register
class LedgerDivergence(FaultTemplate):
    name = "ledger_divergence"
    description = "LEDGER_DATABASE_URL points at a stale snapshot: checkouts succeed but payments silently land in the wrong file."
    forbidden_rules = DEFAULT_FORBIDDEN_RULES

    def inject(self, world: World, seed: int) -> VerificationSpec:
        rng = random.Random((seed * 1_000_003) ^ 0x1ED6)
        correct_value = world.base_env["LEDGER_DATABASE_URL"]

        # ---------------------------------------------------------------- stale snapshot(s) on disk
        snapshot_age_days = rng.randint(3, 9)
        snap_date = world.now - timedelta(days=snapshot_age_days)
        snap_rel = f"data/ledger-snapshot-{snap_date:%Y%m%d}.db"
        self._make_snapshot(world, snap_rel, snap_date)
        extra_dbs = [snap_rel]
        if rng.random() < 0.5:  # an older snapshot too, like a weekly job would leave behind
            old_date = snap_date - timedelta(days=7)
            old_rel = f"data/ledger-snapshot-{old_date:%Y%m%d}.db"
            self._make_snapshot(world, old_rel, old_date)
            extra_dbs.append(old_rel)
        world.extra["extra_dbs"] = extra_dbs
        bad_value = f"sqlite:///{snap_rel}"

        # ---------------------------------------------------------------- timeline
        history_minutes = (world.now - world.history_start).total_seconds() / 60
        lead_minutes = min(rng.uniform(18, 40), max(6.0, history_minutes * 0.45))
        restart_at = world.now - timedelta(minutes=lead_minutes)
        deploy_at = restart_at - timedelta(seconds=rng.uniform(15, 40))
        incident_at = restart_at + timedelta(milliseconds=40)
        page_at = incident_at + timedelta(minutes=rng.uniform(14, 20))  # freshness alerts fire later than error-rate alerts
        page_at = min(page_at, world.now - timedelta(minutes=2))
        support_note_at = min(page_at + timedelta(minutes=rng.uniform(2, 6)), world.now - timedelta(minutes=1))
        lagged = rng.random() < 0.5
        n_base_commits = len(world.commits)

        # ---------------------------------------------------------------- the config change
        message, innocent = rng.choice(_CONFIG_COMMITS)
        env_text = world.env_file.read_text()
        lines = env_text.splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.startswith("LEDGER_DATABASE_URL="))
        lines[idx] = f"LEDGER_DATABASE_URL={bad_value}"
        if innocent:
            k, old, new = innocent
            lines = [f"{k}={new}" if ln.startswith(f"{k}=") else ln for ln in lines]
        new_env = "\n".join(lines) + "\n"
        author = rng.choice(world.team)
        deploys: list[dict] = []
        if lagged:
            commit_at = restart_at - timedelta(minutes=rng.uniform(70, 200))
            config_sha = world.commit_files({".env": new_env}, message, author, commit_at)
            world.commits.append({"sha": config_sha, "message": message.splitlines()[0], "when": util.fmt_iso(commit_at), "author": author["name"]})
            config_deploy_at = commit_at + timedelta(minutes=rng.uniform(2, 6))
            deploys.append({"when": util.fmt_iso(config_deploy_at), "sha": config_sha[:7], "author": author["name"],
                            "message": message.splitlines()[0], "config_only": True, "restart": "deferred"})
            # the innocent release that restarted the service
            ver = self._bump_version(world)
            rel_author = rng.choice(world.team)
            rel_message = rng.choice(_RELEASE_COMMITS).format(ver=ver)
            readme = (world.repo / "README.md").read_text()
            readme_new = readme.rstrip("\n") + "\n\nRelease notes: see CHANGELOG in the wiki.\n"
            rel_commit_at = restart_at - timedelta(minutes=rng.uniform(3, 9))
            head_sha = world.commit_files({"README.md": readme_new, "checkout/__init__.py": f'"""checkout-service: order checkout API."""\n\n__version__ = "{ver}"\n'},
                                          rel_message, rel_author, rel_commit_at)
            world.commits.append({"sha": head_sha, "message": rel_message, "when": util.fmt_iso(rel_commit_at), "author": rel_author["name"]})
            deploys.append({"when": util.fmt_iso(deploy_at), "sha": head_sha[:7], "author": rel_author["name"],
                            "message": rel_message, "config_only": False, "restart": "restart"})
            deploy_commit, deploy_message, deploy_author = head_sha, rel_message, rel_author["name"]
        else:
            commit_at = deploy_at - timedelta(minutes=rng.uniform(2, 9))
            config_sha = world.commit_files({".env": new_env}, message, author, commit_at)
            world.commits.append({"sha": config_sha, "message": message.splitlines()[0], "when": util.fmt_iso(commit_at), "author": author["name"]})
            deploys.append({"when": util.fmt_iso(deploy_at), "sha": config_sha[:7], "author": author["name"],
                            "message": message.splitlines()[0], "config_only": True, "restart": "restart"})
            deploy_commit, deploy_message, deploy_author = config_sha, message.splitlines()[0], author["name"]
        world.fault = self.name

        incident = IncidentProfile(
            commit_at=commit_at, deploy_at=deploy_at, restart_at=restart_at, incident_at=incident_at, page_at=page_at,
            support_note_at=support_note_at, failing_endpoints=[], broken_db="ledger", error_message="",
            health_degraded=False, deploy_commit=deploy_commit, deploy_message=deploy_message, deploy_author=deploy_author,
            config_warnings=[],
            root_cause_summary=(
                f"Commit {config_sha[:7]} ({message.splitlines()[0]}) set LEDGER_DATABASE_URL={bad_value} in {SERVICE_NAME}/.env"
                + (f"; it was shipped with the restart deferred and took effect when release {deploy_commit[:7]} restarted the service"
                   if lagged else "; deploy-bot restarted the service with it")
                + f". Payments since {incident_at:%H:%M} UTC went into {snap_rel}. "
                f"Fix: restore LEDGER_DATABASE_URL={correct_value}, restart {SERVICE_NAME}, and copy the diverted payments back "
                f"(scripts/reconcile_ledger.py --source {snap_rel} --apply)."
            ),
            extra={"payments_db": snap_rel, "deploys": deploys, "n_base_commits": n_base_commits, "lagged": lagged,
                   "snapshot_date": util.fmt_iso(snap_date)},
        )

        # ---------------------------------------------------------------- verification spec
        probe_user = rng.choice(world.sample_user_ids)
        probe_items = [{"sku": s, "quantity": rng.randint(1, 2)} for s in rng.sample(world.skus, k=2)]
        ledger_rel = f"{SERVICE_NAME}/{LEDGER_DB}"
        symptom = [
            Check("health_ok", "http", {"method": "GET", "path": "/health", "expect_status": [200]}, "GET /health returns 200"),
            Check("checkout_payment_in_ledger", "http",
                  {"method": "POST", "path": "/checkout", "expect_status": [201],
                   "body": {"user_id": probe_user, "items": probe_items, "payment_method": "card"}, "response_contains": "order_id",
                   "then_sql": {"db": ledger_rel, "response_key": "order_id",
                                "sql": "SELECT COUNT(*) FROM payments WHERE order_id = :order_id", "expect_min": 1}},
                  "a new checkout's payment is recorded in the real ledger"),
            Check("ledger_complete_since_incident", "ledger_complete",
                  {"core": f"{SERVICE_NAME}/data/checkout.db", "ledger": ledger_rel, "since": util.fmt_iso(incident_at)},
                  "every confirmed order since the incident has a payment in the real ledger (diverted payments backfilled)"),
        ]
        root_cause = [
            Check("env_value_correct", "env_sqlite_path",
                  {"file": f"{SERVICE_NAME}/.env", "key": "LEDGER_DATABASE_URL", "expected_path": ledger_rel},
                  "LEDGER_DATABASE_URL resolves to data/ledger.db"),
            Check("app_code_unchanged", "files_unchanged",
                  {"files": [f"{SERVICE_NAME}/checkout/{f}" for f in ("config.py", "db.py", "main.py", "serve.py", "telemetry.py")]},
                  "fix is in configuration, not hardcoded into application code"),
            Check("ledger_file_in_place", "path_exists", {"path": ledger_rel}, "ledger database still at its original path"),
        ]
        collateral = standard_collateral_checks(SERVICE_NAME, allow=[f"{SERVICE_NAME}/.env"], rules=self.forbidden_rules)
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root_cause, collateral_checks=collateral,
            incident=incident, allowed_changed_files=[f"{SERVICE_NAME}/.env"],
            notes=f"snapshot={snap_rel} lagged={lagged} innocent={innocent[0] if innocent else None}",
        )
        world.extra["fault_params"] = {"target": "LEDGER_DATABASE_URL", "kind": "stale_snapshot", "lagged": lagged,
                                       "snapshot": snap_rel, "innocent_change": innocent[0] if innocent else None,
                                       "commit_variant": message.splitlines()[0][:40]}
        world.save()
        return spec

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _make_snapshot(world: World, rel: str, taken_at) -> None:
        dest = world.repo / rel
        shutil.copyfile(world.ledger_db, dest)
        conn = sqlite3.connect(dest)
        conn.execute("DELETE FROM payments WHERE created_at > ?", (util.fmt_iso(taken_at),))
        conn.commit()
        conn.execute("VACUUM")
        conn.close()
        import os
        import time

        ts = time.mktime(taken_at.timetuple())
        os.utime(dest, (ts, ts))

    @staticmethod
    def _bump_version(world: World) -> str:
        import re

        init = world.repo / "checkout" / "__init__.py"
        m = re.search(r'__version__ = "(\d+)\.(\d+)\.(\d+)"', init.read_text())
        major, minor, patch = (int(x) for x in m.groups())
        return f"{major}.{minor}.{patch + 1}"

    # ------------------------------------------------------------------ page
    def render_page(self, world: World, incident: IncidentProfile, rng) -> str:
        conn = sqlite3.connect(f"file:{world.ledger_db}?mode=ro", uri=True)
        try:
            ledger_count = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        finally:
            conn.close()
        since = incident.incident_at.strftime("%H:%M")
        age_min = int((incident.page_at - incident.incident_at).total_seconds() // 60)
        incident_no = 4000 + rng.randint(100, 899)
        titles = [
            f"[P1] payments-ledger freshness: no new ledger entries for {age_min}m while checkout volume is normal",
            f"[P1] ledger_last_payment_age_seconds > 900 (current {age_min * 60}s) — checkout-service captures not reaching the ledger",
            f"[SEV1] payments ledger stale for {age_min}m; orders still confirming",
        ]
        details = [
            f"ledger_payments_total has been flat at {ledger_count} since {since} UTC while POST /checkout keeps returning 201 at the usual rate.",
            f"Ledger exporter reports last payment at {since} UTC; checkout success rate unchanged. No 5xx on the service.",
            f"Finance freshness monitor: no ledger writes since {since} UTC; order feed from checkout-service looks normal.",
        ]
        notes = [
            "Revenue dashboard shows $0 captured since ~{since} UTC although the order feed is normal. Tonight's reconciliation will fail unless the ledger is complete.",
            "Captured payments are missing from the ledger since roughly {since} UTC; orders are confirming fine. We need the missing records back, not just new ones.",
            "Finance export for the last hour came back empty while sales were happening. Please make sure every captured payment ends up in the ledger.",
        ]
        ack = incident.page_at + (world.now - incident.page_at) * rng.uniform(0.2, 0.5)
        lines = [
            f"[PagerDuty] INCIDENT #{incident_no} — TRIGGERED — P1",
            f"Service:      payments-ledger / {SERVICE_NAME} (production)   Escalation policy: payments-oncall → you",
            f"Title:        {rng.choice(titles)}",
            f"Triggered at: {incident.page_at:%Y-%m-%d %H:%M:%S} UTC   (condition held for 10m before paging)",
            'Alert rule:   ledger_last_payment_age_seconds > 900 and rate(http_requests_total{path="/checkout",status="201"}[10m]) > 0.2',
            f"Details:      {rng.choice(details)}",
            f"Finance note ({incident.support_note_at:%H:%M} UTC, Slack #fin-ops): \"{rng.choice(notes).format(since=since)}\"",
            "Runbook:      (none linked)",
            f"Acknowledged: you, {ack:%H:%M} UTC",
            "",
            page_footer(world),
        ]
        return "\n".join(lines)
