"""Steps 5-6: the harness runs an agent end to end, logs a replayable trajectory, and verifies."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from sregym.cli import main as cli_main
from sregym.harness.agents.scripted import ScriptedAgent
from sregym.harness.episode import EpisodeConfig, run_episode
from sregym.harness.trajectory import read_trajectory
from tests.conftest import HISTORY_MINUTES


def _run(tmp_path: Path, seed: int, mode: str, **kw):
    cfg = EpisodeConfig(seed=seed, out_dir=tmp_path / f"run-{mode}-{seed}", history_minutes=HISTORY_MINUTES,
                        workdir=tmp_path / "worlds", live_traffic=False, **kw)
    return run_episode(ScriptedAgent(mode), cfg)


@pytest.mark.parametrize("seed", [1, 2, 4])
def test_scripted_solver_reaches_full_reward(tmp_path: Path, seed: int):
    res = _run(tmp_path, seed, "solve")
    assert res.success and res.reward == 1.0 and res.stop_reason == "resolved", json.dumps(res.verification, indent=1)
    meta, steps, end = read_trajectory(Path(res.trajectory_path))
    assert meta["seed"] == seed and end["reward"] == 1.0
    assert steps[0]["observation"] == meta["task_prompt"]
    for i, s in enumerate(steps):
        for key in ("observation", "tool_call", "tool_args", "tool_result", "state_hash", "tool_error"):
            assert key in s
        if i:
            assert s["observation"] == steps[i - 1]["tool_result"]
    hashes = [s["state_hash"] for s in steps]
    edit_idx = next(i for i, s in enumerate(steps) if s["tool_call"] == "edit_file")
    assert hashes[edit_idx] != hashes[edit_idx - 1]  # editing .env changes the state hash
    assert hashes[0] == hashes[1]  # read-only steps do not
    assert (Path(res.trajectory_path).parent / "result.json").exists()
    assert not Path(res.world_root).exists()  # cleaned up


def test_masking_and_workaround_and_noop_score_low(tmp_path: Path):
    mask = _run(tmp_path, 3, "mask")
    assert mask.reward == 0.0 and not mask.verification["symptom_resolved"]
    wa = _run(tmp_path, 3, "workaround")
    assert wa.verification["symptom_resolved"] and not wa.verification["root_cause_fixed"] and wa.reward == 0.15
    noop = _run(tmp_path, 3, "noop")
    assert noop.reward == 0.0 and noop.steps == 1
    sloppy = _run(tmp_path, 3, "sloppy")
    assert sloppy.verification["root_cause_fixed"] and not sloppy.verification["no_collateral_damage"] and sloppy.reward == 0.5


def test_max_steps_and_replay(tmp_path: Path):
    res = _run(tmp_path, 6, "solve", max_steps=4)
    assert res.stop_reason == "max_steps" and res.steps == 4 and res.reward == 0.0
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli_main(["replay", res.trajectory_path]) == 0
    out = buf.getvalue()
    assert "== step 4" in out and "stop_reason=max_steps" in out and "task prompt" in out
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli_main(["replay", res.trajectory_path, "--step", "2", "--full"]) == 0
    assert "== step 2" in buf.getvalue() and "== step 3" not in buf.getvalue()


def test_cli_run_and_verify_kept_world(tmp_path: Path):
    out = tmp_path / "out"
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["run", "--seed", "8", "--agent", "scripted", "--mode", "solve", "--history-minutes", str(HISTORY_MINUTES),
                       "--workdir", str(tmp_path / "w"), "--keep-world", "--out", str(out), "--quiet", "--no-traffic"])
    assert rc == 0 and "reward=1.00" in buf.getvalue()
    result = json.loads((out / "result.json").read_text())
    world_root = result["world_root"]
    assert Path(world_root).exists()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["verify", "--world", world_root, "--trajectory", str(out / "trajectory.jsonl"), "--start-service"])
    assert rc == 0 and "root_cause_fixed=PASS" in buf.getvalue()
