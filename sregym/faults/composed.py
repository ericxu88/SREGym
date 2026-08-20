"""Fault composition: two independent faults in one world, one page.

Two things went wrong this afternoon -- a deploy-borne fault and an environmental one. The page
belongs to whichever alert fired first; the other alert is stacked onto it. The verifier requires
BOTH root causes coherently fixed (plus every symptom probe and the usual collateral rules).

Vetted pairs (one deploy-fault + one no-deploy fault, so deploy/restart evidence stays coherent):

  migration+perms   unapplied_migration + db_file_permissions -- with a real causal ordering when the
                    permissions hit the core db: the migration cannot be applied until the write bit
                    is restored
  ratelimit+perms   rate_limit_misconfig + db_file_permissions(ledger) -- one endpoint shows BOTH 429
                    bursts and readonly 500s, interleaved
  migration+cron    unapplied_migration + cron_write_lock -- steady 500s on one endpoint family and
                    minute-aligned lock bursts on another

Use ``--fault composed`` (the seed picks a pair) or ``--fault composed:<a>+<b>``.
"""
from __future__ import annotations

import random
from typing import Any

from sregym import util
from sregym.faults.base import Check, FaultTemplate, IncidentProfile, VerificationSpec
from sregym.generator.world import World

PAIRS = {
    "migration+perms": ("unapplied_migration", "db_file_permissions"),
    "ratelimit+perms": ("rate_limit_misconfig", "db_file_permissions"),
    "migration+cron": ("unapplied_migration", "cron_write_lock"),
}


def _dedupe(checks: list[Check]) -> list[Check]:
    seen: set[str] = set()
    out: list[Check] = []
    for c in checks:
        key = util.sha256_json({"t": c.type, "p": c.params})
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


class ComposedFault(FaultTemplate):
    description = "Two independent faults, one page: both root causes must be fixed."

    def __init__(self, pair_name: str | None = None):
        self.pair_name = pair_name
        self.name = f"composed:{pair_name}" if pair_name else "composed"
        self._members: list[FaultTemplate] = []

    def inject(self, world: World, seed: int) -> VerificationSpec:
        from sregym.faults.base import get_fault

        rng = random.Random((seed * 1_000_003) ^ 0xC0)
        pair_name = self.pair_name or rng.choice(sorted(PAIRS))
        names = PAIRS[pair_name]
        self._members = [get_fault(n) for n in names]
        specs: list[VerificationSpec] = []
        saved_params: list[dict[str, Any]] = []
        for i, member in enumerate(self._members):
            spec = member.inject(world, seed * 7 + i)
            specs.append(spec)
            saved_params.append(dict(world.extra.get("fault_params", {})))
        world.fault = self.name = f"composed:{pair_name}"

        # ------------------------------------------------------------ merge incident profiles
        a, b = specs[0].incident, specs[1].incident
        primary = a if a.page_at <= b.page_at else b  # the first alert owns the page
        merged_extra: dict[str, Any] = {}
        for inc in (a, b):
            for k, v in inc.extra.items():
                if k == "endpoint_errors":
                    dest = merged_extra.setdefault("endpoint_errors", {})
                    for ep, ov in v.items():
                        dest[ep] = dict(ov, since=util.fmt_iso(inc.incident_at))
                elif k == "lock_burst":
                    merged_extra[k] = dict(v, since=util.fmt_iso(inc.incident_at), endpoints=inc.failing_endpoints)
                elif k == "deploys":
                    merged_extra.setdefault("deploys", []).extend(v)
                elif k == "n_base_commits":
                    merged_extra[k] = min(int(v), int(merged_extra.get(k, v)))
                else:
                    merged_extra.setdefault(k, v)
        merged_extra["members"] = [
            {"fault": name, "incident": spec.incident.to_dict(), "fault_params": params}
            for name, spec, params in zip(names, specs, saved_params)
        ]
        merged_extra["primary"] = names[0] if primary is a else names[1]
        deploy_member = a if a.extra.get("deploys") or not b.extra.get("deploys") else b
        merged = IncidentProfile(
            commit_at=deploy_member.commit_at, deploy_at=deploy_member.deploy_at, restart_at=deploy_member.restart_at,
            incident_at=min(a.incident_at, b.incident_at), page_at=min(a.page_at, b.page_at),
            support_note_at=min(a.support_note_at, b.support_note_at),
            failing_endpoints=sorted(set(a.failing_endpoints) | set(b.failing_endpoints)),
            broken_db=primary.broken_db, error_message=primary.error_message,
            health_degraded=a.health_degraded or b.health_degraded,
            deploy_commit=deploy_member.deploy_commit, deploy_message=deploy_member.deploy_message,
            deploy_author=deploy_member.deploy_author, config_warnings=a.config_warnings + b.config_warnings,
            root_cause_summary="TWO independent faults. " + " AND ".join(
                f"({i + 1}) [{name}] {spec.incident.root_cause_summary}" for i, (name, spec) in enumerate(zip(names, specs))),
            extra=merged_extra,
        )

        # ------------------------------------------------------------ merge specs
        def prefixed(checks: list[Check], tag: str, sibling_allow: list[str]) -> list[Check]:
            """Prefix names; shrink 'unchanged' guards by files the SIBLING fault legitimately changes
            (one member's workaround detector must not veto the other member's required fix)."""
            import fnmatch

            out = []
            for c in checks:
                params = dict(c.params)
                if c.type == "files_unchanged":
                    kept = [f for f in params["files"] if not any(f == a or fnmatch.fnmatch(f, a) for a in sibling_allow)]
                    if not kept:
                        continue
                    params["files"] = kept
                out.append(Check(f"{tag}:{c.name}", c.type, params, c.description))
            return out

        symptom = _dedupe(prefixed(specs[0].symptom_checks, names[0], specs[1].allowed_changed_files)
                          + prefixed(specs[1].symptom_checks, names[1], specs[0].allowed_changed_files))
        root = _dedupe(prefixed(specs[0].root_cause_checks, names[0], specs[1].allowed_changed_files)
                       + prefixed(specs[1].root_cause_checks, names[1], specs[0].allowed_changed_files))
        allow = sorted(set(specs[0].allowed_changed_files) | set(specs[1].allowed_changed_files))
        from sregym.faults.base import standard_collateral_checks

        collateral = standard_collateral_checks(world.naming.service, allow=allow, rules=self.forbidden_rules)
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root, collateral_checks=collateral,
            incident=merged, allowed_changed_files=allow, notes=f"pair={pair_name} members={names}",
        )
        world.extra["fault_params"] = {"target": pair_name, "kind": "+".join(p.get("kind", "?") for p in saved_params),
                                       "innocent_change": None,
                                       "member_kinds": {n: p.get("kind") for n, p in zip(names, saved_params)}}
        world.save()
        return spec

    def finalize(self, world: World, spec: VerificationSpec) -> None:
        for member in self._members:
            member.finalize(world, spec)

    def render_page(self, world: World, incident: IncidentProfile, rng: Any) -> str:
        from sregym.faults.base import get_fault

        members = incident.extra["members"]
        primary_name = incident.extra["primary"]
        primary = next(m for m in members if m["fault"] == primary_name)
        secondary = next(m for m in members if m["fault"] != primary_name)
        page = get_fault(primary["fault"]).render_page(world, IncidentProfile.from_dict(primary["incident"]), rng)
        sec_inc = IncidentProfile.from_dict(secondary["incident"])
        sec_page = get_fault(secondary["fault"]).render_page(world, sec_inc, rng)
        sec_lines = {l.split(":", 1)[0].strip(): l for l in sec_page.splitlines() if ":" in l}
        stacked = "\n".join(filter(None, [
            f"[PagerDuty] ALSO TRIGGERED at {sec_inc.page_at:%H:%M:%S} UTC (separate alert, same service):",
            "  " + sec_lines.get("Title", "Title:        (see alert console)").strip(),
            "  " + sec_lines.get("Details", "").strip() if sec_lines.get("Details") else None,
        ]))
        marker = "\n\nCurrent time is"
        return page.replace(marker, f"\n\n{stacked}{marker}")
