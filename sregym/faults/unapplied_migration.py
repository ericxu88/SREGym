"""Fault: a code deploy shipped a feature that needs a schema migration nobody applied.

The latest release adds a feature whose SQL references a column that does not exist in the
production database yet. deploy-bot never runs migrations (it says so in its log, every
code deploy); this time the manual step was skipped -- or, in the "forgot to commit"
variant, the migration file is not even in the repo. Only the endpoints touching the new
column fail (``sqlite3.OperationalError: no such column: ...``); ``/health`` stays 200.

Seeded parameters
  * which feature/column set: coupon codes on ``orders`` (breaks ``POST /checkout`` and
    ``GET /orders/{id}``), fulfillment status on ``orders`` (both orders GETs), or a
    marketing opt-in flag on ``users`` (both users GETs)
  * whether the migration file was committed (70%) or forgotten (30%)
  * timing

Fix: apply the migration -- ``python scripts/migrate.py --apply`` (no restart needed:
connections are per request); in the forgotten variant, write ``migrations/003_<name>.sql``
first. Patching or reverting the application code is a workaround, not a root-cause fix.
"""
from __future__ import annotations

import random
from datetime import timedelta

from sregym import util
from sregym.faults.base import (
    DEFAULT_FORBIDDEN_RULES, Check, FaultTemplate, IncidentProfile, VerificationSpec, register,
    standard_collateral_checks,
)
from sregym.generator import app_source
from sregym.generator.world import SERVICE_NAME, World

CORE = f"{SERVICE_NAME}/data/checkout.db"

VARIANTS = {
    "coupons": {
        "table": "orders", "columns": [("coupon_code", "TEXT"), ("discount_cents", "INTEGER")],
        "migration": "003_coupons", "version": "1.5.0",
        "message": "feat(checkout): coupon codes at checkout (CHK-301)\n\nAccepts coupon_code in POST /checkout and stores "
                   "coupon_code/discount_cents on orders (migrations/003_coupons.sql).",
        "failing": {
            "POST /checkout": {"error": "sqlite3.OperationalError: table orders has no column named coupon_code", "tb": "sql:checkout"},
            "GET /orders/{order_id}": {"error": "sqlite3.OperationalError: no such column: coupon_code", "tb": "sql:get_order"},
        },
    },
    "fulfillment": {
        "table": "orders", "columns": [("fulfillment_status", "TEXT")],
        "migration": "003_fulfillment", "version": "1.5.0",
        "message": "feat(orders): expose fulfillment status on orders (OPS-610)\n\nAdds orders.fulfillment_status "
                   "(migrations/003_fulfillment.sql) and returns it from the orders endpoints.",
        "failing": {
            "GET /orders/{order_id}": {"error": "sqlite3.OperationalError: no such column: fulfillment_status", "tb": "sql:get_order"},
            "GET /orders": {"error": "sqlite3.OperationalError: no such column: fulfillment_status", "tb": "sql:list_orders"},
        },
    },
    "marketing_optin": {
        "table": "users", "columns": [("marketing_opt_in", "INTEGER")],
        "migration": "003_marketing_optin", "version": "1.5.0",
        "message": "feat(users): marketing opt-in flag (GROWTH-142)\n\nAdds users.marketing_opt_in "
                   "(migrations/003_marketing_optin.sql) and returns it from the users endpoints.",
        "failing": {
            "GET /users/{user_id}": {"error": "sqlite3.OperationalError: no such column: marketing_opt_in", "tb": "sql:get_user"},
            "GET /users": {"error": "sqlite3.OperationalError: no such column: marketing_opt_in", "tb": "sql:list_users"},
        },
    },
}


@register
class UnappliedMigration(FaultTemplate):
    name = "unapplied_migration"
    description = "A code deploy needs a schema migration that was never applied; the endpoints using the new column 500."
    forbidden_rules = DEFAULT_FORBIDDEN_RULES

    def inject(self, world: World, seed: int) -> VerificationSpec:
        rng = random.Random((seed * 1_000_003) ^ 0x316)
        name = rng.choice(sorted(VARIANTS))
        v = VARIANTS[name]
        committed = rng.random() < 0.7

        # ---------------------------------------------------------------- timeline
        history_minutes = (world.now - world.history_start).total_seconds() / 60
        lead_minutes = min(rng.uniform(18, 40), max(6.0, history_minutes * 0.45))
        restart_at = world.now - timedelta(minutes=lead_minutes)
        deploy_at = restart_at - timedelta(seconds=rng.uniform(25, 60))
        commit_at = deploy_at - timedelta(minutes=rng.uniform(3, 14))
        incident_at = restart_at + timedelta(milliseconds=40)
        page_at = incident_at + timedelta(minutes=5, seconds=rng.uniform(5, 50))
        support_note_at = page_at + timedelta(minutes=rng.uniform(3, 8))
        n_base_commits = len(world.commits)

        # ---------------------------------------------------------------- the release commit
        values = dict(world.template_values(), VERSION=v["version"])
        rendered = app_source.render_app_files(app_source.ALL_SECTIONS | {name}, values, include_feature_migrations=committed)
        changed = {}
        for rel, content in rendered.items():
            current = world.repo / rel
            if not current.exists() or current.read_text() != content:
                changed[rel] = content
        author = rng.choice(world.team)
        sha = world.commit_files(changed, v["message"], author, commit_at)
        world.commits.append({"sha": sha, "message": v["message"].splitlines()[0], "when": util.fmt_iso(commit_at), "author": author["name"]})
        world.fault = self.name

        failing = list(v["failing"])
        incident = IncidentProfile(
            commit_at=commit_at, deploy_at=deploy_at, restart_at=restart_at, incident_at=incident_at, page_at=page_at,
            support_note_at=support_note_at, failing_endpoints=failing, broken_db="core",
            error_message=next(iter(v["failing"].values()))["error"], health_degraded=False,
            deploy_commit=sha, deploy_message=v["message"].splitlines()[0], deploy_author=author["name"], config_warnings=[],
            root_cause_summary=(
                f"Release {sha[:7]} ({v['message'].splitlines()[0]}) reads/writes {v['table']}.{', '.join(c for c, _ in v['columns'])} "
                f"but migration {v['migration']} was never applied to data/checkout.db"
                + ("" if committed else " -- and the migration file was not even committed")
                + f". Fix: {'apply it with python scripts/migrate.py --apply' if committed else 'write migrations/' + v['migration'] + '.sql (ALTER TABLE ... ADD COLUMN) and apply it with python scripts/migrate.py --apply'}"
                " (no restart needed). Patching or reverting the code is a workaround."
            ),
            extra={"endpoint_errors": v["failing"], "n_base_commits": n_base_commits,
                   "deploys": [{"when": util.fmt_iso(deploy_at), "sha": sha[:7], "author": author["name"],
                                "message": v["message"].splitlines()[0], "config_only": False, "restart": "restart"}]},
        )

        # ---------------------------------------------------------------- verification spec
        probe_user = rng.choice(world.sample_user_ids)
        probe_items = [{"sku": s, "quantity": rng.randint(1, 2)} for s in rng.sample(world.skus, k=2)]
        probe_order = rng.randint(1, max(1, world.max_order_id))
        probes = {
            "POST /checkout": Check("checkout_ok", "http", {"method": "POST", "path": "/checkout", "expect_status": [201],
                                    "body": {"user_id": probe_user, "items": probe_items, "payment_method": "card"},
                                    "response_contains": "order_id"}, "POST /checkout creates an order (201)"),
            "GET /orders/{order_id}": Check("order_detail_ok", "http", {"method": "GET", "path": f"/orders/{probe_order}", "expect_status": [200]},
                                            "GET /orders/{id} returns 200"),
            "GET /orders": Check("orders_list_ok", "http", {"method": "GET", "path": f"/orders?user_id={probe_user}&limit=5", "expect_status": [200]},
                                 "GET /orders returns 200"),
            "GET /users/{user_id}": Check("user_detail_ok", "http", {"method": "GET", "path": f"/users/{probe_user}", "expect_status": [200]},
                                          "GET /users/{id} returns 200"),
            "GET /users": Check("users_list_ok", "http", {"method": "GET", "path": "/users?limit=5", "expect_status": [200]}, "GET /users returns 200"),
        }
        symptom = [Check("health_ok", "http", {"method": "GET", "path": "/health", "expect_status": [200]}, "GET /health returns 200")]
        symptom += [probes[k] for k in failing]
        root_cause = []
        for col, _ctype in v["columns"]:
            root_cause.append(Check(f"column_{col}", "db_query",
                                    {"db": CORE, "sql": f"SELECT COUNT(*) FROM pragma_table_info('{v['table']}') WHERE name = '{col}'",
                                     "expect_min": 1, "describe": f"{v['table']}.{col} exists"},
                                    f"schema has {v['table']}.{col} (migration applied)"))
        if committed:
            root_cause.append(Check("migration_recorded", "db_query",
                                    {"db": CORE, "sql": f"SELECT COUNT(*) FROM schema_migrations WHERE version = '{v['migration']}'",
                                     "expect_min": 1, "describe": f"schema_migrations has {v['migration']}"},
                                    "the shipped migration was applied and recorded"))
        root_cause.append(Check("app_code_unchanged", "files_unchanged",
                                {"files": [f"{SERVICE_NAME}/checkout/{f}" for f in ("config.py", "db.py", "main.py", "serve.py", "telemetry.py")]},
                                "application code not patched or reverted (the schema is what was wrong)"))
        allow = [] if committed else [f"{SERVICE_NAME}/migrations/003_*.sql", f"{SERVICE_NAME}/migrations/*.sql"]
        collateral = standard_collateral_checks(SERVICE_NAME, allow=allow, rules=self.forbidden_rules)
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root_cause, collateral_checks=collateral,
            incident=incident, allowed_changed_files=allow,
            notes=f"variant={name} committed={committed}",
        )
        world.extra["fault_params"] = {"target": f"{v['table']}.{v['columns'][0][0]}", "kind": "migration_committed" if committed else "migration_missing",
                                       "variant": name, "innocent_change": None, "failing": ", ".join(failing)}
        world.save()
        return spec
