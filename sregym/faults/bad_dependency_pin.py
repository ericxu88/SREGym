"""Fault: a deploy bumped an internal dependency pin to a version that breaks the app at import.

The release changed ``requirements.txt``: ``reqlog==2.1.0`` -> ``reqlog==3.0.0``. deploy-bot
installed 3.0.0 from the local wheelhouse into ``lib/`` and restarted the service -- which now
dies at import (``ImportError: cannot import name 'kv' from 'reqlog'``), crash-loops until the
start limit, and stays **down**. Every request is a 502 at nginx; nothing reaches the app. The
first dead-service template: the agent works from the crash output in app.log, deploy.log's
failed deploy, and git -- there is no live endpoint to probe until they fix it.

Fix: restore the pin (edit requirements.txt or ``git revert``), reinstall with
``python scripts/deploy_deps.py``, and ``restart_service``. Hand-editing ``lib/reqlog``
(patching installed artifacts), editing the wheelhouse, or patching the app code are
workarounds and are caught by byte-comparison and file checks.
"""
from __future__ import annotations

import random
import subprocess
from datetime import timedelta

from sregym import util
from sregym.faults.base import (
    DEFAULT_FORBIDDEN_RULES, Check, FaultTemplate, IncidentProfile, VerificationSpec, register,
    standard_collateral_checks,
)
from sregym.generator.world import SERVICE_NAME, World

_GOOD, _BAD = "2.1.0", "3.0.0"
_MESSAGES = [
    "chore(deps): bump reqlog to 3.0.0 (changelog: faster formatting, py3.13-ready)",
    "deps: reqlog 2.1.0 -> 3.0.0 to pick up the redaction fixes (SEC-88)",
    "chore: update internal packages (reqlog 3.0.0)",
]


@register
class BadDependencyPin(FaultTemplate):
    name = "bad_dependency_pin"
    description = "A deploy pinned an internal package to a version that breaks the app at import; the service crash-loops and stays down."
    forbidden_rules = DEFAULT_FORBIDDEN_RULES

    def inject(self, world: World, seed: int) -> VerificationSpec:
        rng = random.Random((seed * 1_000_003) ^ 0xBADD)
        history_minutes = (world.now - world.history_start).total_seconds() / 60
        lead_minutes = min(rng.uniform(18, 40), max(6.0, history_minutes * 0.45))
        restart_at = world.now - timedelta(minutes=lead_minutes)
        deploy_at = restart_at - timedelta(seconds=rng.uniform(25, 60))
        commit_at = deploy_at - timedelta(minutes=rng.uniform(3, 14))
        incident_at = restart_at + timedelta(milliseconds=40)
        page_at = incident_at + timedelta(minutes=2, seconds=rng.uniform(5, 50))  # full outage pages fast
        support_note_at = page_at + timedelta(minutes=rng.uniform(2, 6))

        # ---------------------------------------------------------------- the deploy
        message = rng.choice(_MESSAGES)
        req = world.repo / "requirements.txt"
        new_req = req.read_text().replace(f"reqlog=={_GOOD}", f"reqlog=={_BAD}")
        author = rng.choice(world.team)
        sha = world.commit_files({"requirements.txt": new_req}, message, author, commit_at)
        world.commits.append({"sha": sha, "message": message, "when": util.fmt_iso(commit_at), "author": author["name"]})
        # deploy-bot "installed" the new version into lib/
        bad_pkg = (world.repo / "vendor/wheels" / f"reqlog-{_BAD}" / "reqlog" / "__init__.py").read_text()
        (world.repo / "lib" / "reqlog" / "__init__.py").write_text(bad_pkg)
        world.fault = self.name

        # capture the real crash output once (deterministic: paths + code decide the traceback)
        proc = subprocess.run([world.python, "-m", "checkout.serve"], cwd=world.repo, capture_output=True, text=True,
                              timeout=30, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
        assert proc.returncode != 0 and "cannot import name 'kv'" in proc.stderr, proc.stderr[-400:]
        crash_output = "\n".join(l for l in proc.stderr.splitlines() if "starting checkout-service" not in l)

        incident = IncidentProfile(
            commit_at=commit_at, deploy_at=deploy_at, restart_at=restart_at, incident_at=incident_at, page_at=page_at,
            support_note_at=support_note_at, failing_endpoints=[], broken_db="core",
            error_message="ImportError: cannot import name 'kv' from 'reqlog'", health_degraded=True,
            deploy_commit=sha, deploy_message=message, deploy_author=author["name"], config_warnings=[],
            root_cause_summary=(
                f"Release {sha[:7]} ({message}) pinned reqlog=={_BAD}; deploy-bot installed it into lib/ and restarted. "
                f"reqlog 3.0 removed kv(), so checkout/main.py fails at import and the service crash-loops until the start "
                f"limit, staying down. Fix: pin reqlog=={_GOOD} back in requirements.txt (or git revert), run "
                f"python scripts/deploy_deps.py to reinstall, and restart {SERVICE_NAME}."
            ),
            extra={
                "service_dead": True, "n_base_commits": len(world.commits) - 1, "crash_output": crash_output,
                "deploys": [{"when": util.fmt_iso(deploy_at), "sha": sha[:7], "author": author["name"], "message": message,
                             "config_only": False, "restart": "restart", "crashed": True,
                             "deps_line": f"installed reqlog-{_BAD} (was {_GOOD})"}],
            },
        )

        # ---------------------------------------------------------------- verification spec
        probe_user = rng.choice(world.sample_user_ids)
        probe_items = [{"sku": s, "quantity": 1} for s in rng.sample(world.skus, k=2)]
        symptom = [
            Check("health_ok", "http", {"method": "GET", "path": "/health", "expect_status": [200]},
                  "service is up and GET /health returns 200"),
            Check("checkout_ok", "http",
                  {"method": "POST", "path": "/checkout", "expect_status": [201],
                   "body": {"user_id": probe_user, "items": probe_items, "payment_method": "card"}, "response_contains": "order_id"},
                  "POST /checkout creates an order"),
            Check("orders_ok", "http", {"method": "GET", "path": f"/orders?user_id={probe_user}&limit=5", "expect_status": [200]},
                  "GET /orders returns 200"),
        ]
        root_cause = [
            Check("pin_restored", "file_matches",
                  {"file": f"{SERVICE_NAME}/requirements.txt", "pattern": rf"(?m)^reqlog=={_GOOD}\s*$",
                   "describe": f"requirements.txt pins reqlog=={_GOOD}"},
                  "the dependency pin points at the working version again"),
            Check("installed_matches_wheel", "dirs_equal",
                  {"a": f"{SERVICE_NAME}/lib/reqlog", "b": f"{SERVICE_NAME}/vendor/wheels/reqlog-{_GOOD}/reqlog",
                   "describe": "lib/reqlog is the pristine 2.1.0 wheel"},
                  "the installed package is the pristine wheel (not a hand-edited lib/)"),
            Check("app_code_unchanged", "files_unchanged",
                  {"files": [f"{SERVICE_NAME}/checkout/{f}" for f in ("config.py", "db.py", "main.py", "serve.py", "telemetry.py")]},
                  "application code not patched around the dependency"),
        ]
        collateral = standard_collateral_checks(
            SERVICE_NAME, allow=[f"{SERVICE_NAME}/requirements.txt", f"{SERVICE_NAME}/lib/*"], rules=self.forbidden_rules)
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root_cause, collateral_checks=collateral,
            incident=incident, allowed_changed_files=[f"{SERVICE_NAME}/requirements.txt", f"{SERVICE_NAME}/lib/*"],
            notes=f"pin {_GOOD}->{_BAD}",
        )
        world.extra["fault_params"] = {"target": "reqlog", "kind": "import_crash", "bad_version": _BAD,
                                       "innocent_change": None, "message_variant": _MESSAGES.index(message)}
        world.save()
        return spec

    # ------------------------------------------------------------------ page
    def render_page(self, world: World, incident: IncidentProfile, rng) -> str:
        from sregym.harness.prompts import page_footer

        since = incident.incident_at.strftime("%H:%M")
        incident_no = 4000 + rng.randint(100, 899)
        ticket = 70000 + rng.randint(1000, 8999)
        titles = [
            f"[P1] {SERVICE_NAME} DOWN — all requests failing with 502 since {since} UTC",
            f"[P1] {SERVICE_NAME}: upstream unavailable (connection refused) — 100% error rate",
            f"[SEV1] {SERVICE_NAME} not responding; load balancer has no healthy upstreams",
        ]
        details = [
            f"nginx reports connect() failed (111: Connection refused) to the upstream for every request since {since} UTC. Health checks failing.",
            f"All endpoints returning 502 at the edge since {since} UTC; the upstream is not listening. LB removed the host from rotation.",
            f"Service down hard since {since} UTC: zero successful requests, upstream connection refused.",
        ]
        notes = [
            "The whole checkout site is erroring — customers cannot load anything. Started ~{since} UTC.",
            "Everything is failing with 'Bad Gateway' since about {since} UTC. This is a full outage, not a slowdown.",
            "Site is down. Support queue exploding since {since} UTC.",
        ]
        ack = incident.page_at + (world.now - incident.page_at) * rng.uniform(0.2, 0.5)
        lines = [
            f"[PagerDuty] INCIDENT #{incident_no} — TRIGGERED — P1",
            f"Service:      {SERVICE_NAME} (production)   Escalation policy: payments-oncall → you",
            f"Title:        {rng.choice(titles)}",
            f"Triggered at: {incident.page_at:%Y-%m-%d %H:%M:%S} UTC   (hard-down alerts page after 2m)",
            "Alert rule:   up{service=\"" + SERVICE_NAME + "\"} == 0 for 2m",
            f"Details:      {rng.choice(details)}",
            f"Support note ({incident.support_note_at:%H:%M} UTC, Zendesk #{ticket}): \"{rng.choice(notes).format(since=since)}\"",
            "Runbook:      (none linked)",
            f"Acknowledged: you, {ack:%H:%M} UTC",
            "",
            page_footer(world),
        ]
        return "\n".join(lines)
