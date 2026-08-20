"""Fault: deploy-bot died mid-write shipping ``.env`` -- the file on disk is truncated.

A legitimate config-only commit (an innocent value tweak) was deployed, but the ship
step's write was interrupted partway: everything from the seeded cut point down is gone
(sometimes ending mid-line). Git has the correct file at HEAD; the working tree does not.
The app boots (the application block with APP_PORT survives) and silently falls back to
dev defaults for every lost key:

  * ``DATABASE_URL``/``LEDGER_DATABASE_URL`` -> ``data/<pkg>-dev.db`` (missing) -> every
    affected request 500s with ``unable to open database file``; /health 503
  * ``LOG_PATH`` -> empty -> the restarted process logs to stderr: **app.log goes dark at
    exactly the restart** (it ends with the old process's shutdown lines). The 500s are
    only visible in the nginx access log and the metrics -- the missing traceback is the
    fingerprint.
  * the rate limit, session and webhook secrets fall back to dev values too

Variants: cut after the ``# --- databases`` header (both DBs break, all endpoints 500)
or after the ``DATABASE_URL`` line (ledger only: reads fine, checkout 500s).

Fix: restore ``.env`` to the shipped configuration (e.g. ``git checkout -- .env``) and
restart. Restoring only the database line(s) leaves logging dark and the dev secrets in
production -- the file is the broken object, and the fix is the whole file.
"""
from __future__ import annotations

import random
from datetime import timedelta

from sregym import util
from sregym.faults.base import DEFAULT_FORBIDDEN_RULES, Check, FaultTemplate, IncidentProfile, VerificationSpec, register, standard_collateral_checks
from sregym.generator.world import World

_ERROR = "sqlite3.OperationalError: unable to open database file"

_INNOCENT = [
    ("CART_TTL_MINUTES", "45", "60", "ops: extend cart TTL to 60m for the promo weekend (GROWTH-77)"),
    ("DATABASE_TIMEOUT_SECONDS", "5", "8", "ops: raise db busy timeout to 8s after the slow-query review (OPS-509)"),
    ("PAYMENT_GATEWAY_TIMEOUT_MS", "1500", "2000", "ops: bump gateway timeout to 2000ms per p99 review"),
]


@register
class TruncatedEnv(FaultTemplate):
    name = "truncated_env"
    description = "Deploy-bot died mid-write: .env is truncated on disk, the app falls back to dev defaults and app.log goes dark at the restart."
    forbidden_rules = DEFAULT_FORBIDDEN_RULES

    def inject(self, world: World, seed: int) -> VerificationSpec:
        rng = random.Random((seed * 1_000_003) ^ 0x7E2C)
        nm = world.naming
        svc, pkg = nm.service, nm.package
        variant = rng.choice(["databases", "ledger"])
        torn = rng.random() < 0.5

        # ---------------------------------------------------------------- timeline
        history_minutes = (world.now - world.history_start).total_seconds() / 60
        lead_minutes = min(rng.uniform(18, 40), max(6.0, history_minutes * 0.45))
        restart_at = world.now - timedelta(minutes=lead_minutes)
        deploy_at = restart_at - timedelta(seconds=rng.uniform(15, 40))
        commit_at = deploy_at - timedelta(minutes=rng.uniform(2, 9))
        incident_at = restart_at + timedelta(milliseconds=40)
        page_at = incident_at + timedelta(minutes=5, seconds=rng.uniform(5, 50))
        support_note_at = page_at + timedelta(minutes=rng.uniform(3, 8))

        # ---------------------------------------------------------------- the innocent commit that got shipped
        key, old, new, message = rng.choice(_INNOCENT)
        env_text = world.env_file.read_text()
        good_env = env_text.replace(f"{key}={old}", f"{key}={new}")
        n_base = len(world.commits)
        author = rng.choice(world.team)
        sha = world.commit_files({".env": good_env}, message, author, commit_at)
        world.commits.append({"sha": sha, "message": message, "when": util.fmt_iso(commit_at), "author": author["name"]})

        # ---------------------------------------------------------------- the interrupted write (working tree only)
        lines = good_env.splitlines(keepends=True)
        if variant == "databases":
            cut = next(i for i, ln in enumerate(lines) if ln.startswith("# --- databases")) + 1
        else:  # ledger: DATABASE_URL survives, everything after is lost
            cut = next(i for i, ln in enumerate(lines) if ln.startswith("DATABASE_URL=")) + 1
        truncated = "".join(lines[:cut])
        if torn:  # the write died mid-line
            nxt = next((ln for ln in lines[cut:] if ln.strip()), "")
            if len(nxt) > 12:
                truncated += nxt[: rng.randint(8, len(nxt) - 5)]
        world.env_file.write_text(truncated)
        world.fault = self.name

        if variant == "databases":
            failing = ["POST /checkout", "GET /orders/{order_id}", "GET /orders", "GET /users/{user_id}", "GET /users"]
            broken = "core"
        else:
            failing = ["POST /checkout"]
            broken = "ledger"
        incident = IncidentProfile(
            commit_at=commit_at, deploy_at=deploy_at, restart_at=restart_at, incident_at=incident_at,
            page_at=page_at, support_note_at=support_note_at, failing_endpoints=failing, broken_db=broken,
            error_message=_ERROR, health_degraded=True, deploy_commit=sha, deploy_message=message,
            deploy_author=author["name"], config_warnings=[],
            root_cause_summary=(
                f"Deploy {sha[:7]} ({message}) shipped {svc}/.env but the write was interrupted: the file on disk is "
                f"truncated after the "
                + ("'# --- databases' header" if variant == "databases" else "DATABASE_URL line")
                + f" ({len(truncated)}/{len(good_env)} bytes). Every lost key silently fell back to its dev default, so "
                + ("both databases point at missing dev files and all DB endpoints 500"
                   if variant == "databases" else "the ledger points at a missing dev file and POST checkout 500s")
                + "; LOG_PATH fell back to stderr so app.log stops at the restart's shutdown lines. Git has the correct "
                f"file at HEAD. Fix: restore {svc}/.env to the committed configuration (e.g. git checkout -- .env) and "
                f"restart {svc}."
            ),
            extra={
                "app_log_dark_since": util.fmt_iso(restart_at - timedelta(milliseconds=50)),
                "n_base_commits": n_base,
                "deploys": [{
                    "when": util.fmt_iso(deploy_at), "sha": sha[:7], "author": author["name"], "message": message,
                    "config_only": True, "restart": "restart",
                    "ship_note": f"WARN: destination write interrupted (connection reset by peer); wrote {len(truncated)}/{len(good_env)} bytes; continuing",
                }],
            },
        )

        # ---------------------------------------------------------------- verification spec
        probe_user = rng.choice(world.sample_user_ids)
        probe_items = [{"sku": s, "quantity": 1} for s in rng.sample(world.skus, k=2)]
        correct_webhook = world.base_env["WEBHOOK_SIGNING_SECRET"]
        symptom = [
            Check("health_ok", "http", {"method": "GET", "path": "/health", "expect_status": [200]},
                  "GET /health returns 200 (both databases reachable)"),
            Check("checkout_ok", "http",
                  {"method": "POST", "path": "/checkout", "expect_status": [201],
                   "body": {"user_id": probe_user, "items": probe_items, "payment_method": "card"},
                   "response_contains": "order_id"},
                  "POST /checkout creates an order (201)"),
            Check("orders_ok", "http", {"method": "GET", "path": f"/orders?user_id={probe_user}&limit=5", "expect_status": [200]},
                  "GET /orders returns 200"),
        ]
        env_rel = f"{svc}/.env"
        root_cause = [
            Check("core_url_restored", "env_sqlite_path",
                  {"file": env_rel, "key": "DATABASE_URL", "expected_path": f"{svc}/{nm.core_db_rel}"},
                  "DATABASE_URL resolves to the production core db again"),
            Check("ledger_url_restored", "env_sqlite_path",
                  {"file": env_rel, "key": "LEDGER_DATABASE_URL", "expected_path": f"{svc}/{nm.ledger_db_rel}"},
                  "LEDGER_DATABASE_URL resolves to the production ledger again"),
            Check("logging_restored", "file_matches",
                  {"file": env_rel, "pattern": r"(?m)^LOG_PATH=logs/app\.log\s*$"},
                  "LOG_PATH restored (logging is part of the shipped config, not optional)"),
            Check("rate_limit_restored", "file_matches",
                  {"file": env_rel, "pattern": r"(?m)^RATE_LIMIT_PER_MINUTE=(?:[6-9][0-9]|[1-9][0-9]{2,})\s*$"},
                  "the production rate limit is back (dev default is not production config)"),
            Check("webhook_secret_restored", "file_matches",
                  {"file": env_rel, "pattern": rf"(?m)^WEBHOOK_SIGNING_SECRET={correct_webhook}\s*$"},
                  "the gateway-shared webhook secret is back (dev default rejects every settlement)"),
            Check("app_code_unchanged", "files_unchanged",
                  {"files": [f"{svc}/{pkg}/{f}" for f in ("config.py", "db.py", "main.py", "serve.py", "telemetry.py")]},
                  "fix is the configuration file, not code fallbacks"),
            Check("db_files_in_place", "path_exists", {"path": f"{svc}/{nm.core_db_rel}"},
                  "core db still at its original path (not moved to match the dev default)"),
        ]
        collateral = standard_collateral_checks(svc, allow=[env_rel], rules=self.forbidden_rules)
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root_cause, collateral_checks=collateral,
            incident=incident, allowed_changed_files=[env_rel],
            notes=f"variant={variant} torn={torn} innocent={key}",
        )
        world.extra["fault_params"] = {"variant": variant, "torn": torn, "innocent_change": key}
        world.save()
        return spec
