"""Rung-3 template #8: disk_full -- writes fail with 'database or disk is full', reads/health fine, quota fix."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sregym.harness.agents.scripted import ScriptedAgent
from sregym.harness.episode import EpisodeConfig, run_episode
from sregym.harness.prompts import build_task_prompt
from sregym.runtime.services import ServiceManager
from sregym.scenario import prepare_world
from sregym.verifier.verify import verify
from tests.conftest import FIXED_NOW, HISTORY_MINUTES


@pytest.mark.parametrize("seed", [1, 5])
def test_inject_is_coherent(tmp_path: Path, seed: int):
    world, spec = prepare_world(seed=seed, fault="disk_full", root=tmp_path / f"w{seed}", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        fp = world.extra["fault_params"]
        assert 0 < fp["quota"] < fp["pages_at_inject"]
        # the committed .env carries the quota, and the deploy commit is the tip
        assert f"DATABASE_MAX_PAGES={fp['quota']}" in world.env_file.read_text()
        assert world.git("show", "HEAD", "--stat").count(".env") >= 1
        # after finalize's VACUUM the quota is clamped below the current size
        conn = sqlite3.connect(f"file:{world.core_db}?mode=ro", uri=True)
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        conn.close()
        assert pages > fp["quota"]
        log = world.app_log.read_text()
        assert "database or disk is full" in log
        after = spec.incident.incident_at.strftime("%Y-%m-%d %H:%M")
        lines = [l for l in log.splitlines() if l[:16] >= after and ".access " in l]
        assert any(" POST /checkout 500 " in l for l in lines)
        assert not any(" /health 503" in l for l in lines)  # health stays green
        page = build_task_prompt(world, spec.incident, fault=spec.fault).lower()
        for forbidden in ("disk", "quota", "max_pages", "page_count", "database or disk is full"):
            assert forbidden not in page, forbidden
    finally:
        world.destroy()


def test_writes_fail_live_and_fix_is_config(tmp_path: Path):
    world, spec = prepare_world(seed=3, fault="disk_full", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    sm = ServiceManager(world)
    assert "listening" in sm.start(announce=False)
    try:
        res = verify(world, spec)
        assert (res.symptom_resolved, res.root_cause_fixed, res.no_collateral_damage) == (False, False, True), res.summary()
        assert any(c.name == "health_ok" and c.passed for c in res.checks)     # health never broke
        assert any(c.name == "orders_ok" and c.passed for c in res.checks)     # reads never broke
        assert any(c.name == "checkout_writes_again" and not c.passed for c in res.checks)
        env = world.env_file
        quota = world.extra["fault_params"]["quota"]
        # raising the quota only a little is not a durable fix
        env.write_text(env.read_text().replace(f"DATABASE_MAX_PAGES={quota}", f"DATABASE_MAX_PAGES={quota * 4}"))
        sm.restart()
        res = verify(world, spec)
        assert not res.root_cause_fixed, res.summary()
        # the real fix: remove the quota line, restart
        env.write_text("".join(l for l in env.read_text().splitlines(keepends=True) if "DATABASE_MAX_PAGES" not in l))
        sm.restart()
        res = verify(world, spec)
        assert res.success and res.reward == 1.0, res.summary()
    finally:
        sm.close()
        world.destroy()


def test_scripted_solver_fully_resolves(tmp_path: Path):
    res = run_episode(ScriptedAgent("solve"), EpisodeConfig(seed=4, fault="disk_full", out_dir=tmp_path / "run",
                                                            history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.success and res.reward == 1.0, json.dumps(res.verification, indent=1)


def test_code_patch_is_a_workaround(tmp_path: Path):
    res = run_episode(ScriptedAgent("workaround"), EpisodeConfig(seed=4, fault="disk_full", out_dir=tmp_path / "wa",
                                                                 history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w2", live_traffic=False))
    assert res.verification["symptom_resolved"] and not res.verification["root_cause_fixed"]
    assert res.reward < 0.5


def test_mask_fails(tmp_path: Path):
    res = run_episode(ScriptedAgent("mask"), EpisodeConfig(seed=2, fault="disk_full", out_dir=tmp_path / "mask",
                                                           history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.reward == 0.0 and not res.verification["symptom_resolved"]
