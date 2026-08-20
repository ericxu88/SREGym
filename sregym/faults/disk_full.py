"""Fault: a capacity-guardrail deploy set the core database's page quota below its size.

The app supports an optional ``DATABASE_MAX_PAGES`` guardrail (``PRAGMA max_page_count``
on core connections; unset/0 = unlimited, documented in the repo README). An ops commit
added it to ``.env`` with a value computed wrong -- below the database's *current* page
count. SQLite clamps the quota to the current size, so every write that needs a new page
fails with the genuine ``sqlite3.OperationalError: database or disk is full``.

The error actively misleads: ``df`` shows plenty of space, permissions are fine, reads
and ``/health`` stay green -- only writes (``POST /checkout``) fail, from the moment of
the config deploy's restart. Fix: remove ``DATABASE_MAX_PAGES`` from ``.env`` (or raise
it far above the database size) and restart. Deleting rows to fit under the quota or
patching the pragma out of the code are workarounds, not fixes.

Seeded parameters: the committed quota value, the commit message, timing.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import timedelta

from sregym import util
from sregym.faults.base import DEFAULT_FORBIDDEN_RULES, Check, FaultTemplate, IncidentProfile, VerificationSpec, register
from sregym.generator.world import World

_ERROR = "sqlite3.OperationalError: database or disk is full"

_COMMITS = [
    "ops: cap core database growth pending the archive job rollout (OPS-733)",
    "ops(db): enforce a size quota on the core database after last week's runaway-growth incident",
    "chore: add DATABASE_MAX_PAGES guardrail ahead of the storage tier migration (INFRA-291)",
]

# plausible human-picked round numbers for a quota (pages)
_ROUND = [200, 250, 256, 300, 320, 384, 400, 500, 512, 600, 640, 750, 800, 1000, 1024, 1500, 2000, 2048]


def _page_count(db) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA page_count").fetchone()[0])
    finally:
        conn.close()


@register
class DiskFull(FaultTemplate):
    name = "disk_full"
    description = "A guardrail deploy set DATABASE_MAX_PAGES below the core db's size: reads and /health fine, every write fails with 'database or disk is full'."
    forbidden_rules = DEFAULT_FORBIDDEN_RULES

    def inject(self, world: World, seed: int) -> VerificationSpec:
        rng = random.Random((seed * 1_000_003) ^ 0xD15C)
        nm = world.naming
        svc, pkg = nm.service, nm.package
        pages_now = _page_count(world.core_db)
        candidates = [v for v in _ROUND if pages_now * 0.35 <= v <= pages_now * 0.85]
        quota = rng.choice(candidates) if candidates else max(64, int(pages_now * 0.6))
        variant = rng.randrange(len(_COMMITS))
        message = _COMMITS[variant]

        # ---------------------------------------------------------------- timeline
        history_minutes = (world.now - world.history_start).total_seconds() / 60
        lead_minutes = min(rng.uniform(18, 40), max(6.0, history_minutes * 0.45))
        restart_at = world.now - timedelta(minutes=lead_minutes)
        deploy_at = restart_at - timedelta(seconds=rng.uniform(15, 40))
        commit_at = deploy_at - timedelta(minutes=rng.uniform(2, 9))
        incident_at = restart_at + timedelta(milliseconds=40)
        page_at = incident_at + timedelta(minutes=5, seconds=rng.uniform(5, 50))
        support_note_at = page_at + timedelta(minutes=rng.uniform(3, 8))

        # ---------------------------------------------------------------- mutate .env (add the quota line)
        env_text = world.env_file.read_text()
        lines = env_text.splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.startswith("DATABASE_TIMEOUT_SECONDS="))
        lines.insert(idx + 1, f"DATABASE_MAX_PAGES={quota}")
        new_env = "\n".join(lines) + "\n"
        author = rng.choice(world.team)
        sha = world.commit_files({".env": new_env}, message, author, commit_at)
        world.commits.append({"sha": sha, "message": message, "when": util.fmt_iso(commit_at), "author": author["name"]})
        world.fault = self.name

        incident = IncidentProfile(
            commit_at=commit_at, deploy_at=deploy_at, restart_at=restart_at, incident_at=incident_at,
            page_at=page_at, support_note_at=support_note_at,
            failing_endpoints=["POST /checkout"], broken_db="core", error_message=_ERROR,
            health_degraded=False, deploy_commit=sha, deploy_message=message, deploy_author=author["name"],
            config_warnings=[],
            root_cause_summary=(
                f"Deploy {sha[:7]} ({message}) added DATABASE_MAX_PAGES={quota} to {svc}/.env -- below the core "
                f"database's current size (~{pages_now} pages), so SQLite clamps the quota to the current size and "
                f"every write that needs a new page fails with '{_ERROR.split(': ')[1]}' while reads and /health stay "
                f"green. Disk space is NOT low. Fix: remove DATABASE_MAX_PAGES (or set it far above the database "
                f"size) in {svc}/.env and restart {svc}."
            ),
            extra={"endpoint_errors": {"POST /checkout": {"error": _ERROR, "tb": "sql:checkout"}}},
        )

        # ---------------------------------------------------------------- verification spec
        probe_user = rng.choice(world.sample_user_ids)
        probe_items = [{"sku": s, "quantity": 1} for s in rng.sample(world.skus, k=2)]
        symptom = [
            Check("health_ok", "http", {"method": "GET", "path": "/health", "expect_status": [200]},
                  "GET /health returns 200"),
            Check("checkout_writes_again", "http_burst",
                  {"method": "POST", "path": "/checkout", "expect_status": [201], "n": 4,
                   "body": {"user_id": probe_user, "items": probe_items, "payment_method": "card"}},
                  "4 consecutive checkouts succeed (core + ledger writes allocate pages freely)"),
            Check("orders_ok", "http", {"method": "GET", "path": f"/orders?user_id={probe_user}&limit=5", "expect_status": [200]},
                  "GET /orders returns 200"),
        ]
        quota_pattern = r"(?m)^\s*DATABASE_MAX_PAGES\s*="
        root_cause = [
            Check("quota_lifted", "any_of", {"options": [
                {"name": "quota_removed", "checks": [
                    {"name": "no_quota_line", "type": "file_not_matches",
                     "params": {"file": f"{svc}/.env", "pattern": quota_pattern,
                                "describe": "DATABASE_MAX_PAGES removed from .env"}},
                ]},
                {"name": "quota_raised_far_above_db", "checks": [
                    {"name": "quota_generous", "type": "file_matches",
                     "params": {"file": f"{svc}/.env", "pattern": r"(?m)^DATABASE_MAX_PAGES=(0|[1-9][0-9]{4,})\s*$",
                                "describe": "DATABASE_MAX_PAGES set to 0 (unlimited) or >= 10000 pages (>= ~13x the db)"}},
                ]},
            ]}, "the quota is gone, disabled, or far above the database's size"),
            Check("app_code_unchanged", "files_unchanged",
                  {"files": [f"{svc}/{pkg}/{f}" for f in ("config.py", "db.py", "main.py", "serve.py", "telemetry.py")]},
                  "fix is in configuration, not a code patch around the guardrail"),
            Check("db_file_in_place", "path_exists", {"path": f"{svc}/{nm.core_db_rel}"},
                  "core db still at its original path"),
        ]
        from sregym.faults.base import standard_collateral_checks

        collateral = standard_collateral_checks(svc, allow=[f"{svc}/.env"], rules=self.forbidden_rules)
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root_cause, collateral_checks=collateral,
            incident=incident, allowed_changed_files=[f"{svc}/.env"],
            notes=f"quota={quota} pages_at_inject={pages_now} commit_variant={variant}",
        )
        world.extra["fault_params"] = {"quota": quota, "pages_at_inject": pages_now, "commit_variant": variant}
        world.save()
        return spec

    def finalize(self, world: World, spec: VerificationSpec) -> None:
        """VACUUM the core db after history generation: packed pages mean the (clamped) quota
        bites on the next insert, so live write failures start immediately and deterministically."""
        conn = sqlite3.connect(world.core_db)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
