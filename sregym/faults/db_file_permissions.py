"""Fault: a host-hardening policy made the database path read-only -- writes fail, reads work.

The fleet configuration-management agent ("fleetd") applied a permissions baseline that was
written for hosts where the service data lives elsewhere: it stripped the write bit from the
service's data directory or one of the database files. SQLite happily keeps *reading* (it
falls back to read-only), ``/health``'s connectivity check passes, every GET works -- but any
write fails immediately with ``sqlite3.OperationalError: attempt to write a readonly database``,
so ``POST /checkout`` 500s. No deploy, no restart, no git change: the trail is fleetd.log
(the applied rule, old -> new mode) and ``ls -la``.

Seeded target: the ``data/`` directory (0555), ``data/checkout.db`` (0444) or
``data/ledger.db`` (0444) -- same symptom, different ``ls -la`` finding.

Fix: restore owner write with ``chmod`` (e.g. ``chmod 755 data`` / ``chmod 644 data/checkout.db``).
Repointing ``.env`` somewhere writable or patching the app is a workaround.

Note: reproduces only when the harness does not run as root (root ignores file modes).
"""
from __future__ import annotations

import os
import random
from datetime import timedelta

from sregym.faults.base import (
    DEFAULT_FORBIDDEN_RULES, Check, FaultTemplate, IncidentProfile, VerificationSpec, register,
    standard_collateral_checks,
)
from sregym import util
from sregym.generator.world import SERVICE_NAME, World

_ERROR = "sqlite3.OperationalError: attempt to write a readonly database"

VARIANTS = {
    "data_dir": {"rel": "data", "dir": True, "bad_mode": 0o555, "good_mode": 0o755,
                 "rule": "restrict-service-data-dirs", "tb": "sql:checkout"},
    "core_file": {"rel": "data/checkout.db", "dir": False, "bad_mode": 0o444, "good_mode": 0o644,
                  "rule": "worldwritable-db-files", "tb": "sql:checkout"},
    "ledger_file": {"rel": "data/ledger.db", "dir": False, "bad_mode": 0o444, "good_mode": 0o644,
                    "rule": "worldwritable-db-files", "tb": "sql:checkout_ledger_sql"},
}


@register
class DbFilePermissions(FaultTemplate):
    name = "db_file_permissions"
    description = "A hardening policy made the DB path read-only: reads and /health fine, every checkout write 500s."
    forbidden_rules = DEFAULT_FORBIDDEN_RULES

    def inject(self, world: World, seed: int) -> VerificationSpec:
        rng = random.Random((seed * 1_000_003) ^ 0xD8)
        name = rng.choice(sorted(VARIANTS))
        v = VARIANTS[name]
        target = world.repo / v["rel"]

        history_minutes = (world.now - world.history_start).total_seconds() / 60
        lead_minutes = min(rng.uniform(18, 40), max(6.0, history_minutes * 0.45))
        incident_at = world.now - timedelta(minutes=lead_minutes)
        page_at = incident_at + timedelta(minutes=5, seconds=rng.uniform(5, 50))
        support_note_at = page_at + timedelta(minutes=rng.uniform(3, 8))

        old_mode = target.stat().st_mode & 0o777
        # the chmod itself happens in finalize(): the history generator must still be able to write
        world.fault = self.name
        head = world.commits[-1]

        rel_from_root = f"{SERVICE_NAME}/{v['rel']}"
        incident = IncidentProfile(
            commit_at=incident_at, deploy_at=incident_at, restart_at=incident_at, incident_at=incident_at,
            page_at=page_at, support_note_at=support_note_at, failing_endpoints=["POST /checkout"], broken_db="core",
            error_message=_ERROR, health_degraded=False,
            deploy_commit=head["sha"], deploy_message=head["message"], deploy_author=head["author"], config_warnings=[],
            root_cause_summary=(
                f"fleetd applied permissions baseline rule '{v['rule']}' at {incident_at:%H:%M} and set {rel_from_root} to "
                f"{v['bad_mode']:03o} (was {old_mode:03o}). SQLite falls back to read-only, so reads and /health work but every "
                f"write fails with '{_ERROR.split(': ')[1]}'. Fix: chmod {v['good_mode']:03o} {v['rel']} (no restart needed)."
            ),
            extra={
                "no_restart": True, "deploys": [], "n_base_commits": len(world.commits),
                "apply_modes": [{"rel": v["rel"], "mode": v["bad_mode"], "mtime": incident_at.timestamp()}],
                "endpoint_errors": {"POST /checkout": {"error": _ERROR, "tb": v["tb"]}},
                "fleetd_events": [
                    (util.fmt_iso(incident_at - timedelta(seconds=rng.uniform(1, 4))),
                     "policy sync: perms-baseline-v3 drift detected on 1 path"),
                    (util.fmt_iso(incident_at),
                     f"chmod {v['bad_mode']:03o} {target} (was {old_mode:03o}) [rule: {v['rule']}]"),
                    (util.fmt_iso(incident_at + timedelta(seconds=rng.uniform(0.5, 2))),
                     "policy sync completed: 1 change (perms-baseline-v3, pkg-inventory-v9)"),
                ],
            },
        )

        probe_user = rng.choice(world.sample_user_ids)
        probe_items = [{"sku": s, "quantity": 1} for s in rng.sample(world.skus, k=2)]
        symptom = [
            Check("health_ok", "http", {"method": "GET", "path": "/health", "expect_status": [200]}, "GET /health returns 200"),
            Check("checkout_writes_again", "http",
                  {"method": "POST", "path": "/checkout", "expect_status": [201],
                   "body": {"user_id": probe_user, "items": probe_items, "payment_method": "card"}, "response_contains": "order_id"},
                  "POST /checkout creates an order (core + ledger writes succeed)"),
            Check("reads_still_ok", "http", {"method": "GET", "path": f"/users/{probe_user}", "expect_status": [200]},
                  "GET /users/{id} returns 200"),
        ]
        root_cause = [
            Check("path_writable_again", "path_writable", {"path": rel_from_root, "expect_dir": v["dir"]},
                  f"{rel_from_root} is writable again"),
            Check("env_unchanged", "files_unchanged", {"files": [f"{SERVICE_NAME}/.env"]},
                  "database URLs not repointed somewhere writable"),
            Check("app_code_unchanged", "files_unchanged",
                  {"files": [f"{SERVICE_NAME}/checkout/{f}" for f in ("config.py", "db.py", "main.py", "serve.py", "telemetry.py")]},
                  "application code unchanged (permissions were the problem)"),
            Check("db_files_in_place", "path_exists", {"path": f"{SERVICE_NAME}/data/checkout.db"}, "core db at its original path"),
        ]
        collateral = standard_collateral_checks(SERVICE_NAME, allow=[], rules=self.forbidden_rules)
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root_cause, collateral_checks=collateral,
            incident=incident, allowed_changed_files=[], notes=f"variant={name} old_mode={old_mode:03o}",
        )
        world.extra["fault_params"] = {"target": rel_from_root, "kind": name, "bad_mode": f"{v['bad_mode']:03o}",
                                       "innocent_change": None}
        world.save()
        return spec

    def finalize(self, world: World, spec: VerificationSpec) -> None:
        for entry in spec.incident.extra.get("apply_modes", []):
            target = world.repo / entry["rel"]
            os.chmod(target, int(entry["mode"]))
            os.utime(target, (entry["mtime"], entry["mtime"]))
