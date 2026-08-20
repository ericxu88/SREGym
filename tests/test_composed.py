"""Fault composition: two faults, one page, both root causes required."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from sregym.faults.composed import PAIRS
from sregym.faults.unapplied_migration import VARIANTS as MIG_VARIANTS
from sregym.harness.prompts import build_task_prompt
from sregym.runtime.services import ServiceManager
from sregym.scenario import prepare_world
from sregym.verifier.verify import verify
from tests.conftest import FIXED_NOW, HISTORY_MINUTES

_SIGNATURES = {
    "unapplied_migration": ("no such column", "has no column"),
    "cron_write_lock": ("database is locked",),
    "db_file_permissions": ("readonly database",),
    "rate_limit_misconfig": ("exceeded", "429"),
}


@pytest.mark.parametrize("pair", sorted(PAIRS))
def test_pairs_generate_both_evidence_trails(tmp_path: Path, pair: str):
    world, spec = prepare_world(seed=5, fault=f"composed:{pair}", root=tmp_path / pair.replace("+", "_"),
                                now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        names = PAIRS[pair]
        log = world.app_log.read_text()
        for member in names:
            assert any(sig in log for sig in _SIGNATURES[member]), member
        page = build_task_prompt(world, spec.incident, fault=spec.fault)
        assert "ALSO TRIGGERED" in page
        for forbidden in ("migration", "cron", "chmod", "rate_limit_per_minute", ".env", "deploy", "schema"):
            assert forbidden.lower() not in page.lower(), forbidden
        # merged spec: prefixed checks from both members, union allow-list, both member kinds recorded
        prefixes = {c.name.split(":", 1)[0] for c in spec.root_cause_checks}
        assert set(names) <= prefixes | {"coherent_end_state"} or all(any(c.name.startswith(n) for c in spec.root_cause_checks) for n in names)
        assert set(world.extra["fault_params"]["member_kinds"]) == set(names)
        assert "TWO independent faults" in spec.incident.root_cause_summary
    finally:
        world.destroy()


def test_verify_requires_both_root_causes(tmp_path: Path):
    """ratelimit+perms: fixing only the rate limit leaves readonly 500s; only both fixes reach 1.0."""
    world, spec = prepare_world(seed=3, fault="composed:ratelimit+perms", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    sm = ServiceManager(world)
    assert "listening" in sm.start(announce=False)
    try:
        res = verify(world, spec)
        assert not res.symptom_resolved and not res.root_cause_fixed and res.no_collateral_damage
        # fix the rate limit only
        env = world.env_file
        env.write_text(re.sub(r"RATE_LIMIT_PER_MINUTE=\d+", "RATE_LIMIT_PER_MINUTE=100", env.read_text()))
        sm.restart()
        res = verify(world, spec)
        names = {c.name: c.passed for c in res.checks}
        assert names["rate_limit_misconfig:limit_sane"] is True
        assert names["db_file_permissions:path_writable_again"] is False
        assert not res.root_cause_fixed and not res.symptom_resolved  # checkout writes still fail
        # fix the permissions too
        perms_target = [m for m in spec.incident.extra["members"] if m["fault"] == "db_file_permissions"][0]
        rel = perms_target["fault_params"]["target"]
        p = world.root / rel
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
        res = verify(world, spec)
        assert res.success and res.reward == 1.0, res.summary()
    finally:
        sm.close()
        world.destroy()


def test_causal_ordering_migration_blocked_until_chmod(tmp_path: Path):
    """migration+perms with the core db read-only: applying the migration fails until the write bit is back."""
    world, spec = prepare_world(seed=1, fault="composed:migration+perms", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    try:
        perms = [m for m in spec.incident.extra["members"] if m["fault"] == "db_file_permissions"][0]
        assert perms["fault_params"]["kind"] in ("core_file", "data_dir")
        mig = [m for m in spec.incident.extra["members"] if m["fault"] == "unapplied_migration"][0]
        v = MIG_VARIANTS[mig["fault_params"]["variant"]]
        migf = world.repo / "migrations" / f"{v['migration']}.sql"
        if not migf.exists():
            migf.write_text("".join(f"ALTER TABLE {v['table']} ADD COLUMN {c} {t};\n" for c, t in v["columns"])
                            + f"INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES ('{v['migration']}', 'now');\n")
        env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run([world.python, "scripts/migrate.py", "--apply"], cwd=world.repo, capture_output=True, text=True, env=env)
        assert proc.returncode != 0 and ("readonly" in proc.stdout + proc.stderr or "ERROR" in proc.stdout)
        rel = perms["fault_params"]["target"]
        p = world.root / rel
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
        proc = subprocess.run([world.python, "scripts/migrate.py", "--apply"], cwd=world.repo, capture_output=True, text=True, env=env)
        assert proc.returncode == 0 and "applied" in proc.stdout, proc.stdout + proc.stderr
    finally:
        world.destroy()


def test_fault_listing_includes_composed():
    from sregym.faults.base import get_fault, list_faults

    assert "composed" in list_faults()
    with pytest.raises(KeyError):
        get_fault("composed:nope+nope")
