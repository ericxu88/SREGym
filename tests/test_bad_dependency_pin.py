"""Rung-3 template #6: bad_dependency_pin -- dead service at page time, crash-loop supervisor,
wheelhouse reinstall fix, byte-identical install checks."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sregym.harness.agents.scripted import ScriptedAgent
from sregym.harness.episode import EpisodeConfig, run_episode
from sregym.harness.prompts import build_task_prompt
from sregym.runtime.services import ServiceManager
from sregym.scenario import prepare_world
from sregym.verifier.verify import verify
from tests.conftest import FIXED_NOW, HISTORY_MINUTES


@pytest.mark.parametrize("seed", [2, 4])
def test_inject_is_coherent_and_service_is_dead(tmp_path: Path, seed: int):
    world, spec = prepare_world(seed=seed, fault="bad_dependency_pin", root=tmp_path / f"w{seed}", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        assert spec.incident.extra["service_dead"] is True
        assert "reqlog==3.0.0" in (world.repo / "requirements.txt").read_text()
        assert '__version__ = "3.0.0"' in (world.repo / "lib/reqlog/__init__.py").read_text()
        log = world.app_log.read_text()
        assert log.count("ImportError: cannot import name 'kv' from 'reqlog'") == 5  # crash-loop then start limit
        assert "Traceback (most recent call last):" in log
        after = spec.incident.incident_at.strftime("%Y-%m-%d %H:%M")
        assert not any(" checkout.access " in l and l[:16] > after for l in log.splitlines()[-50:])  # nothing served after
        deploy_log = (world.log_dir / "deploy.log").read_text()
        assert "installed reqlog-3.0.0" in deploy_log and "deploy FAILED" in deploy_log and "start-limit-hit" in deploy_log
        rows = [json.loads(l) for l in world.metrics_file.read_text().splitlines() if '"up"' in l]
        assert rows[-1]["v"] == 0.0 and rows[0]["v"] == 1.0
        assert "Connection refused" in (world.root / "var/log/nginx/error.log").read_text()
        page = build_task_prompt(world, spec.incident, fault=spec.fault).lower()
        for forbidden in ("reqlog", "dependency", "pin", "import", "requirements", "deploy", "crash"):
            assert forbidden not in page, forbidden
        assert "502" in page or "refused" in page or "down" in page
    finally:
        world.destroy()


def test_crash_loop_supervisor_and_full_fix(tmp_path: Path):
    world, spec = prepare_world(seed=2, fault="bad_dependency_pin", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    sm = ServiceManager(world)
    try:
        msg = sm.start(announce=False)
        assert "failed to start" in msg and "start limit exceeded" in msg and not sm.is_running()
        assert "start limit exceeded" in (world.log_dir / "deploy.log").read_text()
        assert "failed" in sm.status()
        res = verify(world, spec)
        assert (res.symptom_resolved, res.root_cause_fixed, res.no_collateral_damage) == (False, False, True)
        # hand-editing the installed package is not a root-cause fix even if it revives the service
        lib = world.repo / "lib/reqlog/__init__.py"
        pristine_bad = lib.read_text()
        lib.write_text(pristine_bad + "\n\ndef kv(mapping):\n    return \" \".join(f\"{k}={v}\" for k, v in dict(mapping).items())\n")
        assert "listening" in sm.start(announce=False)
        res = verify(world, spec)
        assert res.symptom_resolved and not res.root_cause_fixed
        assert "no coherent end state" in next(c.detail for c in res.checks if c.name == "coherent_end_state")
        sm.stop()
        # fix-forward is a legitimate root-cause fix: pristine 3.0.0 install + main.py migrated to the new API
        lib.write_text(pristine_bad)
        main = world.repo / "checkout/main.py"
        src = main.read_text()
        main.write_text(src.replace("from reqlog import kv", "from reqlog import fields")
                           .replace("    extra = kv(request.state.log_extra)", "    extra = str(fields(request.state.log_extra))"))
        assert "listening" in sm.start(announce=False)
        res = verify(world, spec)
        assert res.success and res.reward == 1.0, res.summary()
        # ... but dropping the structured fields is not (behavior check catches it)
        sm.stop()
        main.write_text(src.replace("from reqlog import kv", "from reqlog import fields")
                           .replace("    extra = kv(request.state.log_extra)", '    extra = ""'))
        assert "listening" in sm.start(announce=False)
        res = verify(world, spec)
        assert res.symptom_resolved and not res.root_cause_fixed, res.summary()
        # the rollback fix: restore the pin + code, reinstall from the wheelhouse, restart
        sm.stop()
        main.write_text(src)
        req = world.repo / "requirements.txt"
        req.write_text(req.read_text().replace("reqlog==3.0.0", "reqlog==2.1.0"))
        proc = subprocess.run([world.python, "scripts/deploy_deps.py"], cwd=world.repo, capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
        assert proc.returncode == 0 and "installed reqlog-2.1.0" in proc.stdout, proc.stdout + proc.stderr
        assert "listening" in sm.start(announce=False)
        res = verify(world, spec)
        assert res.success and res.reward == 1.0, res.summary()
    finally:
        sm.close()
        world.destroy()


@pytest.mark.parametrize("mode,reward", [("solve", 1.0), ("mask", 0.0)])
def test_scripted_modes(tmp_path: Path, mode: str, reward: float):
    res = run_episode(ScriptedAgent(mode), EpisodeConfig(seed=4, fault="bad_dependency_pin", out_dir=tmp_path / mode,
                                                         history_minutes=HISTORY_MINUTES, workdir=tmp_path / f"w{mode}", live_traffic=False))
    assert res.reward == reward, json.dumps(res.verification, indent=1)


def test_workaround_scores_symptom_only(tmp_path: Path):
    res = run_episode(ScriptedAgent("workaround"), EpisodeConfig(seed=4, fault="bad_dependency_pin", out_dir=tmp_path / "wa",
                                                                 history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.verification["symptom_resolved"] and not res.verification["root_cause_fixed"] and res.reward == 0.3
