"""Fault: a quarterly secret-rotation deploy also rotated the SHARED webhook signing secret.

``WEBHOOK_SIGNING_SECRET`` authenticates the payment gateway's settlement webhooks
(HMAC-SHA256 over the raw body). It is shared with the gateway -- the .env comment block
and the repo README both say to rotate it only in a coordinated change. A routine
"rotate secrets" commit regenerated it anyway (alongside the perfectly legitimate
SESSION_SECRET rotation), so from the deploy's restart every gateway webhook fails
signature validation: 401s on ``POST /webhooks/payments``, a ``<pkg>.webhooks``
WARNING per event, ``webhook_signature_failures_total`` climbing -- and settlements
silently stop being recorded while checkouts keep succeeding. Zero 5xx, /health green.

Fix: restore the previous WEBHOOK_SIGNING_SECRET (it is in git history) and restart.
Rotating SESSION_SECRET was intentional and may stay. Bypassing the signature check in
code is a workaround (``files_unchanged`` catches it).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import random
from datetime import timedelta

from sregym import util
from sregym.faults.base import DEFAULT_FORBIDDEN_RULES, Check, FaultTemplate, IncidentProfile, VerificationSpec, register, standard_collateral_checks
from sregym.generator.world import World

_COMMITS = [
    "chore: rotate service secrets (quarterly policy, SEC-88)",
    "chore(sec): quarterly credential rotation",
    "sec: rotate SESSION_SECRET and webhook secret per rotation calendar",
]


@register
class StaleSecret(FaultTemplate):
    name = "stale_secret"
    description = "A rotation deploy also rotated the gateway-shared webhook secret: settlement webhooks 401 and settlements silently stop. No 5xx."
    forbidden_rules = DEFAULT_FORBIDDEN_RULES

    def inject(self, world: World, seed: int) -> VerificationSpec:
        rng = random.Random((seed * 1_000_003) ^ 0x5EC4)
        nm = world.naming
        svc, pkg = nm.service, nm.package
        correct = world.base_env["WEBHOOK_SIGNING_SECRET"]
        bad = "whsec_%028x" % rng.getrandbits(112)
        new_session = "%032x" % rng.getrandbits(128)
        variant = rng.randrange(len(_COMMITS))
        message = _COMMITS[variant]

        # ---------------------------------------------------------------- timeline
        history_minutes = (world.now - world.history_start).total_seconds() / 60
        lead_minutes = min(rng.uniform(20, 45), max(6.0, history_minutes * 0.45))
        restart_at = world.now - timedelta(minutes=lead_minutes)
        deploy_at = restart_at - timedelta(seconds=rng.uniform(15, 40))
        commit_at = deploy_at - timedelta(minutes=rng.uniform(2, 9))
        incident_at = restart_at + timedelta(milliseconds=40)
        page_at = incident_at + timedelta(minutes=rng.uniform(12, 18))  # freshness alerts fire slowly
        page_at = min(page_at, world.now - timedelta(minutes=2))
        support_note_at = min(page_at + timedelta(minutes=rng.uniform(2, 6)), world.now - timedelta(minutes=1))

        # ---------------------------------------------------------------- mutate .env (rotate both secrets)
        env_text = world.env_file.read_text()
        lines = env_text.splitlines()
        si = next(i for i, ln in enumerate(lines) if ln.startswith("SESSION_SECRET="))
        wi = next(i for i, ln in enumerate(lines) if ln.startswith("WEBHOOK_SIGNING_SECRET="))
        lines[si] = f"SESSION_SECRET={new_session}"
        lines[wi] = f"WEBHOOK_SIGNING_SECRET={bad}"
        new_env = "\n".join(lines) + "\n"
        author = rng.choice(world.team)
        sha = world.commit_files({".env": new_env}, message, author, commit_at)
        world.commits.append({"sha": sha, "message": message, "when": util.fmt_iso(commit_at), "author": author["name"]})
        world.fault = self.name

        incident = IncidentProfile(
            commit_at=commit_at, deploy_at=deploy_at, restart_at=restart_at, incident_at=incident_at,
            page_at=page_at, support_note_at=support_note_at,
            failing_endpoints=["POST /webhooks/payments"], broken_db="ledger", error_message="",
            health_degraded=False, deploy_commit=sha, deploy_message=message, deploy_author=author["name"],
            config_warnings=[],
            root_cause_summary=(
                f"Deploy {sha[:7]} ({message}) rotated WEBHOOK_SIGNING_SECRET in {svc}/.env to a new value the "
                f"payment gateway does not know (the secret is SHARED with the gateway; both the .env comment and "
                f"the README say to rotate it only in a coordinated change). Every settlement webhook since the "
                f"restart fails HMAC validation with 401, so settlements stopped being recorded while checkouts "
                f"keep succeeding. The SESSION_SECRET rotation in the same commit was legitimate. Fix: restore "
                f"WEBHOOK_SIGNING_SECRET={correct} (from git history) in {svc}/.env and restart {svc}."
            ),
            extra={},
        )

        # ---------------------------------------------------------------- verification spec
        probe_user = rng.choice(world.sample_user_ids)
        probe_items = [{"sku": s, "quantity": 1} for s in rng.sample(world.skus, k=2)]
        probe_ref = "whprobe_%016x" % rng.getrandbits(64)
        # keys in ALPHABETICAL order: the spec round-trips through write_json(sort_keys=True), and the
        # signature must cover exactly the bytes the verifier will send (json.dumps of the reloaded dict)
        probe_event = {"amount_cents": 1999, "event": "capture.settled", "gateway_ref": probe_ref,
                       "order_id": world.max_order_id + rng.randint(5000, 9000)}
        probe_sig = "sha256=" + hmac.new(correct.encode(), json.dumps(probe_event).encode(), hashlib.sha256).hexdigest()
        ledger_rel = f"{svc}/{nm.ledger_db_rel}"
        symptom = [
            Check("health_ok", "http", {"method": "GET", "path": "/health", "expect_status": [200]},
                  "GET /health returns 200"),
            Check("checkout_ok", "http",
                  {"method": "POST", "path": "/checkout", "expect_status": [201],
                   "body": {"user_id": probe_user, "items": probe_items, "payment_method": "card"},
                   "response_contains": "order_id"},
                  "POST /checkout still creates orders (checkouts never broke)"),
            Check("webhook_accepted", "http",
                  {"method": "POST", "path": "/webhooks/payments", "expect_status": [200],
                   "body": probe_event, "headers": {"X-Signature": probe_sig},
                   "response_contains": "recorded"},
                  "a gateway-signed settlement webhook is accepted again (200)"),
            Check("settlement_recorded", "db_query",
                  {"db": ledger_rel,
                   "sql": f"SELECT COUNT(*) FROM settlements WHERE gateway_ref = '{probe_ref}'",
                   "expect_min": 1},
                  "the probe settlement landed in the ledger's settlements table"),
        ]
        root_cause = [
            Check("webhook_secret_restored", "file_matches",
                  {"file": f"{svc}/.env", "pattern": rf"(?m)^WEBHOOK_SIGNING_SECRET={correct}\s*$"},
                  "WEBHOOK_SIGNING_SECRET restored to the gateway-shared value"),
            Check("app_code_unchanged", "files_unchanged",
                  {"files": [f"{svc}/{pkg}/{f}" for f in ("config.py", "db.py", "main.py", "serve.py", "telemetry.py")]},
                  "fix is in configuration, not a bypass of signature validation"),
        ]
        collateral = standard_collateral_checks(svc, allow=[f"{svc}/.env"], rules=self.forbidden_rules)
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root_cause, collateral_checks=collateral,
            incident=incident, allowed_changed_files=[f"{svc}/.env"],
            notes=f"commit_variant={variant}",
        )
        world.extra["fault_params"] = {"commit_variant": variant}
        world.save()
        return spec

    # ------------------------------------------------------------------ page
    def render_page(self, world: World, incident: IncidentProfile, rng) -> str:
        from sregym.harness.prompts import page_footer

        svc = world.naming.service
        since = incident.incident_at.strftime("%H:%M")
        age_min = int((incident.page_at - incident.incident_at).total_seconds() // 60)
        incident_no = 4000 + rng.randint(100, 899)
        titles = [
            f"[P2] payments settlement lag: no settlements recorded for {age_min}m while capture volume is normal",
            f"[P2] ledger_last_settlement_age_seconds > 900 (current {age_min * 60}s) on {svc}",
            f"[P2] settlement feed stale for {age_min}m; orders and captures look normal",
        ]
        details = [
            f"ledger_settlements_total has been flat since {since} UTC while checkout captures continue at the usual rate. No 5xx on the service; /health green.",
            f"Settlement exporter reports the last recorded settlement at {since} UTC; capture volume unchanged since then.",
            f"Finance's settlement reconciliation feed is empty since {since} UTC although the order feed is normal.",
        ]
        notes = [
            "Finance: tonight's settlement reconciliation will come up short unless recording resumes — captures since ~{since} UTC have no settlement rows.",
            "The gateway's dashboard shows settlement deliveries going out normally. Our side stopped recording them around {since} UTC.",
            "Settlement report for the last hour is empty while sales kept coming in. Please get settlement recording working again.",
        ]
        ack = incident.page_at + (world.now - incident.page_at) * rng.uniform(0.2, 0.5)
        lines = [
            f"[PagerDuty] INCIDENT #{incident_no} — TRIGGERED — P2",
            f"Service:      payments-settlements / {svc} (production)   Escalation policy: payments-oncall → you",
            f"Title:        {rng.choice(titles)}",
            f"Triggered at: {incident.page_at:%Y-%m-%d %H:%M:%S} UTC   (condition held for 10m before paging)",
            "Alert rule:   ledger_last_settlement_age_seconds > 900 and rate(http_requests_total{status=\"201\"}[10m]) > 0.2",
            f"Details:      {rng.choice(details)}",
            f"Finance note ({incident.support_note_at:%H:%M} UTC, Slack #fin-ops): \"{rng.choice(notes).format(since=since)}\"",
            "Runbook:      (none linked)",
            f"Acknowledged: you, {ack:%H:%M} UTC",
            "",
            page_footer(world),
        ]
        return "\n".join(lines)
