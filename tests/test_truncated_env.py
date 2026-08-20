"""Rung-3 template #10: truncated_env -- interrupted .env ship, dev-default fallbacks, dark app.log."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sregym import util
from sregym.harness.agents.scripted import ScriptedAgent
from sregym.harness.episode import EpisodeConfig, run_episode
from sregym.harness.prompts import build_task_prompt
from sregym.runtime.services import ServiceManager
from sregym.scenario import prepare_world
from sregym.verifier.verify import verify
from tests.conftest import FIXED_NOW, HISTORY_MINUTES


@pytest.mark.parametrize("seed,variant", [(1, "databases"), (2, "ledger")])
def test_inject_is_coherent(tmp_path: Path, seed: int, variant: str):
    world, spec = prepare_world(seed=seed, fault="truncated_env", root=tmp_path / f"w{seed}", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        assert world.extra["fault_params"]["variant"] == variant
        disk = world.env_file.read_text()
        head = world.git("show", "HEAD:.env")
        assert len(disk) < len(head)                    # truncated on disk
        assert head.startswith(disk[: disk.rfind("\n") + 1])  # a prefix of the committed file
        assert "LOG_PATH=" not in disk and "SESSION_SECRET=" not in disk
        parsed = util.parse_env_file(disk)
        if variant == "databases":
            assert parsed.get("DATABASE_URL") != world.base_env["DATABASE_URL"]  # lost or torn
        else:
            assert parsed.get("DATABASE_URL") == world.base_env["DATABASE_URL"]  # core survived
        assert parsed.get("LEDGER_DATABASE_URL") != world.base_env["LEDGER_DATABASE_URL"]
        # app.log ends before the incident: the restarted process logged to stderr
        log = world.app_log.read_text()
        last_ts = log.splitlines()[-1][:23]
        assert last_ts < spec.incident.incident_at.strftime("%Y-%m-%d %H:%M:%S")
        assert "Finished server process" in "\n".join(log.splitlines()[-4:])
        # ... but nginx kept logging the 500s and the deploy log carries the interrupted write
        after = spec.incident.incident_at.strftime("%d/%b/%Y:%H:%M")
        nginx = (world.root / "var/log/nginx/access.log").read_text()
        assert any(" 500 " in l for l in nginx.splitlines()[-200:])
        deploy = (world.log_dir / "deploy.log").read_text()
        assert "write interrupted" in deploy and f"{len(disk)}/{len(head)} bytes" in deploy
        assert world.git("status", "--porcelain").strip() == "M .env"
        page = build_task_prompt(world, spec.incident, fault=spec.fault).lower()
        for forbidden in ("truncat", ".env", "dev default", "log_path", "interrupted"):
            assert forbidden not in page, forbidden
    finally:
        world.destroy()


def test_verifier_requires_full_restore(tmp_path: Path):
    world, spec = prepare_world(seed=2, fault="truncated_env", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    sm = ServiceManager(world)
    assert "listening" in sm.start(announce=False)
    try:
        res = verify(world, spec)
        assert (res.symptom_resolved, res.root_cause_fixed, res.no_collateral_damage) == (False, False, True), res.summary()
        # re-adding just the database URLs resolves the symptom but is not the fix
        head = world.git("show", "HEAD:.env")
        urls = [l for l in head.splitlines() if l.startswith(("DATABASE_URL=", "LEDGER_DATABASE_URL="))]
        disk = world.env_file.read_text()
        world.env_file.write_text(disk + "\n" + "\n".join(urls) + "\n")
        sm.restart()
        res = verify(world, spec)
        assert res.symptom_resolved and not res.root_cause_fixed, res.summary()
        assert any(c.name == "logging_restored" and not c.passed for c in res.checks)
        # the real fix: restore the committed file
        world.env_file.write_text(head)
        sm.restart()
        res = verify(world, spec)
        assert res.success and res.reward == 1.0, res.summary()
    finally:
        sm.close()
        world.destroy()


@pytest.mark.parametrize("seed", [1, 2])  # one seed per variant
def test_scripted_solver_fully_resolves(tmp_path: Path, seed: int):
    res = run_episode(ScriptedAgent("solve"), EpisodeConfig(seed=seed, fault="truncated_env", out_dir=tmp_path / f"run{seed}",
                                                            history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.success and res.reward == 1.0, json.dumps(res.verification, indent=1)


def test_partial_restore_is_incomplete(tmp_path: Path):
    res = run_episode(ScriptedAgent("workaround"), EpisodeConfig(seed=4, fault="truncated_env", out_dir=tmp_path / "wa",
                                                                 history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w2", live_traffic=False))
    assert res.verification["symptom_resolved"] and not res.verification["root_cause_fixed"]
    assert res.reward < 0.5


def test_mask_fails(tmp_path: Path):
    res = run_episode(ScriptedAgent("mask"), EpisodeConfig(seed=3, fault="truncated_env", out_dir=tmp_path / "mask",
                                                           history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.reward == 0.0 and not res.verification["symptom_resolved"]
