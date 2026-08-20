"""Rung-3 template #7: rate_limit_misconfig -- pure-4xx policy incident, burst probes."""
from __future__ import annotations

import json
import re
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


@pytest.mark.parametrize("seed", [3, 5])
def test_inject_is_coherent_and_pure_4xx(tmp_path: Path, seed: int):
    world, spec = prepare_world(seed=seed, fault="rate_limit_misconfig", root=tmp_path / f"w{seed}", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        env = util.parse_env_file(world.env_file.read_text())
        assert int(env["RATE_LIMIT_PER_MINUTE"]) <= 3
        log = world.app_log.read_text()
        after = spec.incident.incident_at.strftime("%Y-%m-%d %H:%M")
        n429 = len(re.findall(r"POST /checkout 429 ", log))
        assert n429 >= 3 and log.count("checkout.ratelimit") == n429
        assert not any(" 429 " in l for l in log.splitlines() if l[:16] < after)  # none before the deploy
        assert " 500 " not in log and "Traceback" not in log  # pure policy incident
        rows = [json.loads(l) for l in world.metrics_file.read_text().splitlines() if "rate_limited" in l]
        assert rows and all(util.parse_iso(r["ts"]) >= spec.incident.incident_at.replace(second=0, microsecond=0) for r in rows)
        page = build_task_prompt(world, spec.incident, fault=spec.fault).lower()
        for forbidden in ("rate_limit_per_minute", ".env", "config", "deploy", "limit was", "600"):
            assert forbidden not in page, forbidden
        assert "429" in page or "too many" in page
    finally:
        world.destroy()


def test_verifier_burst_probe_and_sane_range(tmp_path: Path):
    world, spec = prepare_world(seed=3, fault="rate_limit_misconfig", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    sm = ServiceManager(world)
    assert "listening" in sm.start(announce=False)
    try:
        res = verify(world, spec)
        assert (res.symptom_resolved, res.root_cause_fixed, res.no_collateral_damage) == (False, False, True), res.summary()
        assert any(c.name == "retry_burst_not_limited" and not c.passed and "429" in c.detail for c in res.checks)
        env = world.env_file
        # too-low "fix" (e.g. 30) is still not sane
        env.write_text(re.sub(r"RATE_LIMIT_PER_MINUTE=\d+", "RATE_LIMIT_PER_MINUTE=30", env.read_text()))
        sm.restart()
        res = verify(world, spec)
        assert not res.root_cause_fixed  # burst of 6 passes but the configured value is below the sane range
        # the intended value from the commit message passes
        env.write_text(re.sub(r"RATE_LIMIT_PER_MINUTE=\d+", "RATE_LIMIT_PER_MINUTE=100", env.read_text()))
        sm.restart()
        res = verify(world, spec)
        assert res.success and res.reward == 1.0, res.summary()
    finally:
        sm.close()
        world.destroy()


@pytest.mark.parametrize("mode,reward", [("solve", 1.0), ("mask", 0.0)])
def test_scripted_modes(tmp_path: Path, mode: str, reward: float):
    res = run_episode(ScriptedAgent(mode), EpisodeConfig(seed=5, fault="rate_limit_misconfig", out_dir=tmp_path / mode,
                                                         history_minutes=HISTORY_MINUTES, workdir=tmp_path / f"w{mode}", live_traffic=False))
    assert res.reward == reward, json.dumps(res.verification, indent=1)


def test_workaround_patching_the_limiter_fails_root_cause(tmp_path: Path):
    res = run_episode(ScriptedAgent("workaround"), EpisodeConfig(seed=5, fault="rate_limit_misconfig", out_dir=tmp_path / "wa",
                                                                 history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.verification["symptom_resolved"] and not res.verification["root_cause_fixed"]
