"""Fault: a config deploy set the checkout rate limit to a fraction of its intended value.

``RATE_LIMIT_PER_MINUTE`` went from 600 to 1-3 (seeded) -- in the flagship variant the commit
message says "clamp to 100/min" while the diff sets **1** (dropped zeroes). Nothing errors:
``/health`` is green, there are no 5xx at all. But real checkout traffic contains bursts
(double-clicks, client retries, split carts), and every attempt past the limit gets a 429
with a ``checkout.ratelimit`` WARNING and a ``rate_limited_requests_total`` tick. Customers
see "too many requests" when they retry. The first pure-4xx policy incident.

Fix: restore a sane per-user limit (>= 60/min accepted -- the intended 100 or the old 600 both
pass) in ``.env`` and restart. Raising it in code, or special-casing users, is a workaround.
"""
from __future__ import annotations

import random
from datetime import timedelta

from sregym import util
from sregym.faults.base import (
    DEFAULT_FORBIDDEN_RULES, Check, FaultTemplate, IncidentProfile, VerificationSpec, register,
    standard_collateral_checks,
)
from sregym.generator.world import SERVICE_NAME, World

_VARIANTS = [
    {"limit": 1, "message": "sec: clamp checkout rate limit to 100/min to block card-testing bots (SEC-102)"},
    {"limit": 2, "message": "ops: apply the new per-user rate-limit policy from the Q3 traffic review (RL-7)"},
    {"limit": 3, "message": "ops: tighten checkout rate limiting ahead of the promo weekend"},
]


@register
class RateLimitMisconfig(FaultTemplate):
    name = "rate_limit_misconfig"
    description = "The checkout rate limit was set to 1-3/min instead of ~100; legitimate retry bursts get 429s. No 5xx anywhere."
    forbidden_rules = DEFAULT_FORBIDDEN_RULES

    def inject(self, world: World, seed: int) -> VerificationSpec:
        rng = random.Random((seed * 1_000_003) ^ 0x4429)
        v = rng.choice(_VARIANTS)
        history_minutes = (world.now - world.history_start).total_seconds() / 60
        lead_minutes = min(rng.uniform(18, 40), max(6.0, history_minutes * 0.45))
        restart_at = world.now - timedelta(minutes=lead_minutes)
        deploy_at = restart_at - timedelta(seconds=rng.uniform(15, 40))
        commit_at = deploy_at - timedelta(minutes=rng.uniform(2, 9))
        incident_at = restart_at + timedelta(milliseconds=40)
        page_at = incident_at + timedelta(minutes=rng.uniform(9, 16))  # 429-ratio alerts take longer to accumulate
        page_at = min(page_at, world.now - timedelta(minutes=2))
        support_note_at = min(page_at + timedelta(minutes=rng.uniform(2, 6)), world.now - timedelta(minutes=1))

        env_text = world.env_file.read_text()
        new_env = env_text.replace("RATE_LIMIT_PER_MINUTE=600", f"RATE_LIMIT_PER_MINUTE={v['limit']}")
        assert new_env != env_text
        author = rng.choice(world.team)
        sha = world.commit_files({".env": new_env}, v["message"], author, commit_at)
        world.commits.append({"sha": sha, "message": v["message"], "when": util.fmt_iso(commit_at), "author": author["name"]})
        world.fault = self.name

        incident = IncidentProfile(
            commit_at=commit_at, deploy_at=deploy_at, restart_at=restart_at, incident_at=incident_at, page_at=page_at,
            support_note_at=support_note_at, failing_endpoints=[], broken_db="core", error_message="",
            health_degraded=False, deploy_commit=sha, deploy_message=v["message"], deploy_author=author["name"],
            config_warnings=[],
            root_cause_summary=(
                f"Deploy {sha[:7]} ({v['message']}) set RATE_LIMIT_PER_MINUTE={v['limit']} in {SERVICE_NAME}/.env "
                f"(the commit message suggests ~100 was intended). Legitimate checkout retry bursts now exceed the "
                f"per-user limit and get 429s. Fix: restore a sane value (>= 60, e.g. 100 or 600) and restart."
            ),
            extra={"rate_limit": {"limit": v["limit"], "since": util.fmt_iso(incident_at)}},
        )

        probe_user = rng.choice(world.sample_user_ids)
        probe_items = [{"sku": s, "quantity": 1} for s in rng.sample(world.skus, k=1)]
        symptom = [
            Check("health_ok", "http", {"method": "GET", "path": "/health", "expect_status": [200]}, "GET /health returns 200"),
            Check("retry_burst_not_limited", "http_burst",
                  {"method": "POST", "path": "/checkout", "expect_status": [201], "n": 6,
                   "body": {"user_id": probe_user, "items": probe_items, "payment_method": "card"},
                   "describe": "6 rapid checkouts by one user all succeed (a normal retry burst)"},
                  "a legitimate rapid retry burst is not rate-limited"),
        ]
        root_cause = [
            Check("limit_sane", "file_matches",
                  {"file": f"{SERVICE_NAME}/.env", "pattern": r"(?m)^RATE_LIMIT_PER_MINUTE=(?:[6-9][0-9]|[1-9][0-9]{2,})\s*$",
                   "describe": "RATE_LIMIT_PER_MINUTE is a sane per-user value (>= 60)"},
                  "the configured limit is back in a sane range"),
            Check("app_code_unchanged", "files_unchanged",
                  {"files": [f"{SERVICE_NAME}/checkout/{f}" for f in ("config.py", "db.py", "main.py", "serve.py", "telemetry.py")]},
                  "the limiter itself was not patched"),
        ]
        collateral = standard_collateral_checks(SERVICE_NAME, allow=[f"{SERVICE_NAME}/.env"], rules=self.forbidden_rules)
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root_cause, collateral_checks=collateral,
            incident=incident, allowed_changed_files=[f"{SERVICE_NAME}/.env"],
            notes=f"limit={v['limit']} message={_VARIANTS.index(v)}",
        )
        world.extra["fault_params"] = {"target": "RATE_LIMIT_PER_MINUTE", "kind": f"limit_{v['limit']}",
                                       "innocent_change": None, "message_variant": _VARIANTS.index(v)}
        world.save()
        return spec

    # ------------------------------------------------------------------ page
    def render_page(self, world: World, incident: IncidentProfile, rng) -> str:
        from sregym.harness.prompts import page_footer

        since = incident.incident_at.strftime("%H:%M")
        incident_no = 4000 + rng.randint(100, 899)
        ticket = 70000 + rng.randint(1000, 8999)
        titles = [
            f"[P2] {SERVICE_NAME}: checkout 429 rate elevated since {since} UTC (no 5xx)",
            f"[P2] customers throttled at checkout — 429s climbing since {since} UTC",
            f"[P2] {SERVICE_NAME} rate_limited_requests_total burning since {since} UTC",
        ]
        details = [
            f"rate_limited_requests_total started climbing at {since} UTC and keeps rising. No 5xx, latency normal, /health green.",
            f"A growing share of POST /checkout returns 429 since {since} UTC. All other endpoints and error classes look normal.",
            f"429s on POST /checkout since {since} UTC; conversion dashboards dipping. No deploy alarms fired (no 5xx).",
        ]
        notes = [
            "Customers report 'Too many requests, please try again later' when they retry a card or double-click Buy. Second attempts keep failing.",
            "Support tickets: checkout blocks people who try again after a slow first attempt — they get a 'too many attempts' error since ~{since} UTC.",
            "Users who split their basket into two orders say the second order is rejected with a rate-limit error.",
        ]
        ack = incident.page_at + (world.now - incident.page_at) * rng.uniform(0.2, 0.5)
        lines = [
            f"[PagerDuty] INCIDENT #{incident_no} — TRIGGERED — P2",
            f"Service:      {SERVICE_NAME} (production)   Escalation policy: payments-oncall → you",
            f"Title:        {rng.choice(titles)}",
            f"Triggered at: {incident.page_at:%Y-%m-%d %H:%M:%S} UTC   (condition held for 10m before paging)",
            "Alert rule:   increase(rate_limited_requests_total[10m]) > 20",
            f"Details:      {rng.choice(details)}",
            f"Support note ({incident.support_note_at:%H:%M} UTC, Zendesk #{ticket}): \"{rng.choice(notes).format(since=since)}\"",
            "Runbook:      (none linked)",
            f"Acknowledged: you, {ack:%H:%M} UTC",
            "",
            page_footer(world),
        ]
        return "\n".join(lines)
