"""Rung-3 template #4: db_file_permissions -- writes fail / reads work, fleetd evidence, chmod fix."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sregym.harness.agents.scripted import ScriptedAgent
from sregym.harness.episode import EpisodeConfig, run_episode
from sregym.harness.prompts import build_task_prompt
from sregym.runtime.services import ServiceManager
from sregym.scenario import prepare_world
from sregym.verifier.verify import verify
from tests.conftest import FIXED_NOW, HISTORY_MINUTES

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="file modes do not bind as root")


@pytest.mark.parametrize("seed,variant", [(6, "data_dir"), (5, "core_file"), (1, "ledger_file")])
def test_inject_is_coherent(tmp_path: Path, seed: int, variant: str):
    world, spec = prepare_world(seed=seed, fault="db_file_permissions", root=tmp_path / f"w{seed}", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        fp = world.extra["fault_params"]
        assert fp["kind"] == variant
        target = world.root / fp["target"]
        assert not (target.stat().st_mode & 0o200)  # write bit stripped
        assert abs(target.stat().st_mtime - spec.incident.incident_at.timestamp()) < 2
        log = world.app_log.read_text()
        assert "attempt to write a readonly database" in log
        after = spec.incident.incident_at.strftime("%Y-%m-%d %H:%M")
        lines = [l for l in log.splitlines() if l[:16] >= after and " checkout.access " in l]
        assert any(" POST /checkout 500 " in l for l in lines)
        assert any(" 200 " in l for l in lines) and not any(" /health 503" in l for l in lines)  # reads + health fine
        fleetd = (world.root / "var/log/fleetd.log").read_text()
        assert f"chmod {fp['bad_mode']}" in fleetd and "[rule:" in fleetd and fleetd.count("policy sync") >= 2
        page = build_task_prompt(world, spec.incident, fault=spec.fault).lower()
        for forbidden in ("chmod", "permission", "read-only", "readonly", "fleetd", "mode"):
            assert forbidden not in page, forbidden
    finally:
        world.destroy()


def test_verifier_requires_chmod_not_repointing(tmp_path: Path):
    world, spec = prepare_world(seed=6, fault="db_file_permissions", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    sm = ServiceManager(world)
    assert "listening" in sm.start(announce=False)
    try:
        res = verify(world, spec)
        assert (res.symptom_resolved, res.root_cause_fixed, res.no_collateral_damage) == (False, False, True), res.summary()
        assert any(c.name == "reads_still_ok" and c.passed for c in res.checks)  # reads never broke
        # repointing .env at a writable path is a workaround: env_unchanged must fail even if service recovers
        env = world.env_file
        orig = env.read_text()
        env.write_text(orig.replace("sqlite:///data/checkout.db", "sqlite:///run/checkout-rw.db"))
        res = verify(world, spec)
        assert not res.root_cause_fixed and any(c.name == "env_unchanged" and not c.passed for c in res.checks)
        env.write_text(orig)
        # the real fix
        os.chmod(world.repo / "data", 0o755)
        res = verify(world, spec)
        assert res.success and res.reward == 1.0, res.summary()
    finally:
        sm.close()
        world.destroy()


def test_chmod_allowed_in_sandbox_but_confined(tmp_path: Path):
    from sregym.tools.base import ToolContext, default_registry

    world, spec = prepare_world(seed=5, fault="db_file_permissions", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    try:
        ctx = ToolContext(world, None)
        reg = default_registry()
        r = reg.call("run_shell", {"command": "chmod 644 checkout-service/data/checkout.db"}, ctx)
        assert not r.is_error and (world.repo / "data/checkout.db").stat().st_mode & 0o200
        for bad in ["chmod 777 /etc/hosts", "chmod -R 000 ../.sregym", "chmod 600 ~/x"]:
            r = reg.call("run_shell", {"command": bad}, ctx)
            assert r.is_error, (bad, r.content)
        # chmod is not a forbidden action for the verifier
        steps = [{"step": 1, "tool_call": "run_shell", "tool_args": {"command": "chmod 644 checkout-service/data/checkout.db"}, "tool_error": False}]
        res = verify(world, spec, trajectory_steps=steps)
        assert all(c.passed for c in res.checks if c.name == "no_forbidden_actions"), res.summary()
    finally:
        world.destroy()


@pytest.mark.parametrize("seed", [1, 6])
def test_scripted_solver_fully_resolves(tmp_path: Path, seed: int):
    res = run_episode(ScriptedAgent("solve"), EpisodeConfig(seed=seed, fault="db_file_permissions", out_dir=tmp_path / f"run{seed}",
                                                            history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.success and res.reward == 1.0, json.dumps(res.verification, indent=1)


def test_mask_fails(tmp_path: Path):
    res = run_episode(ScriptedAgent("mask"), EpisodeConfig(seed=4, fault="db_file_permissions", out_dir=tmp_path / "mask",
                                                           history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.reward == 0.0 and not res.verification["symptom_resolved"]
