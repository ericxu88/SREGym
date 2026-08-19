"""Sweep runner: seed parsing, outcome taxonomy, cost, report, concurrency + resume."""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from sregym.cli import main as cli_main
from sregym.harness.sweep import (
    SweepConfig, build_report, classify_outcome, estimate_cost, parse_seeds, run_sweep, wilson_ci,
)
from tests.conftest import HISTORY_MINUTES


def test_parse_seeds():
    assert parse_seeds("1-3,7,5-6,3") == [1, 2, 3, 5, 6, 7]
    with pytest.raises(ValueError):
        parse_seeds("5-1")
    with pytest.raises(ValueError):
        parse_seeds("")


def _res(s, r, c, stop="resolved", infra=None):
    return {"verification": {"symptom_resolved": s, "root_cause_fixed": r, "no_collateral_damage": c},
            "stop_reason": stop, "infra_error": infra}


def test_outcome_taxonomy():
    env_edit = [{"tool_call": "edit_file", "tool_args": {"path": "checkout-service/.env"}, "tool_error": False}]
    assert classify_outcome(_res(True, True, True)) == "success"
    assert classify_outcome(_res(True, True, False)) == "collateral_damage"
    assert classify_outcome(_res(True, False, True)) == "workaround"
    assert classify_outcome(_res(False, True, True)) == "fixed_not_restarted"
    assert classify_outcome(_res(False, False, True), env_edit) == "wrong_fix"
    assert classify_outcome(_res(False, False, True), []) == "masked"
    assert classify_outcome(_res(False, False, True, stop="agent_stopped")) == "gave_up"
    assert classify_outcome(_res(False, False, True, stop="max_steps")) == "never_found"
    assert classify_outcome(_res(False, False, True, stop="token_budget")) == "never_found"
    assert classify_outcome(_res(True, True, True, infra="service did not start")) == "infra_error"
    revert = [{"tool_call": "run_shell", "tool_args": {"command": "git -C checkout-service revert HEAD"}, "tool_error": False}]
    assert classify_outcome(_res(False, False, True, stop="max_steps"), revert) == "wrong_fix"


def test_cost_and_ci():
    usage = {"input_tokens": 1_000_000, "cache_read_input_tokens": 900_000, "cache_creation_input_tokens": 50_000, "output_tokens": 10_000}
    # opus 5: 50k uncached*5 + 900k*0.5 + 50k*6.25 + 10k*25 per 1M
    assert estimate_cost("claude-opus-5", usage) == pytest.approx((50_000 * 5 + 900_000 * 0.5 + 50_000 * 6.25 + 10_000 * 25) / 1e6)
    assert estimate_cost("unknown-model", usage) is None and estimate_cost(None, usage) is None
    lo, hi = wilson_ci(5, 10)
    assert 0.2 < lo < 0.5 < hi < 0.8
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_scripted_sweep_concurrent_and_resumable(tmp_path: Path):
    out = tmp_path / "sweep"
    cfg = SweepConfig(seeds=[1, 2, 3], out_dir=out, agent="scripted", agent_kwargs={"mode": "solve"}, concurrency=3,
                      history_minutes=HISTORY_MINUTES, live_traffic=False)
    lines: list[str] = []
    summary = run_sweep(cfg, progress=lines.append)
    assert summary["n_model_results"] == 3 and summary["success"] == 3 and summary["success_rate"] == 1.0
    assert summary["outcomes"]["success"] == 3 and summary["by_target_kind"]
    assert (out / "report.md").exists() and (out / "summary.json").exists()
    assert sorted(p.name for p in (out / "results").glob("*.json")) == ["seed-1.json", "seed-2.json", "seed-3.json"]
    assert (out / "episodes" / "seed-2" / "trajectory.jsonl").exists()
    # resume: same seeds -> nothing runs; new seed -> only that one runs
    lines.clear()
    cfg2 = SweepConfig(seeds=[1, 2, 3, 4], out_dir=out, agent="scripted", agent_kwargs={"mode": "solve"}, concurrency=2,
                       history_minutes=HISTORY_MINUTES, live_traffic=False)
    summary2 = run_sweep(cfg2, progress=lines.append)
    assert any("resuming: 3 seeds already done, 1 to run" in l for l in lines)
    import re
    assert summary2["n_model_results"] == 4 and sum(1 for l in lines if re.match(r"\[\d+/\d+\]", l)) == 1  # only seed 4 ran
    # report regeneration via CLI
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli_main(["report", str(out)]) == 0
    assert "success rate" in buf.getvalue() and "By env var" in buf.getvalue()


def test_sweep_classifies_masking(tmp_path: Path):
    out = tmp_path / "sweep-mask"
    cfg = SweepConfig(seeds=[5], out_dir=out, agent="scripted", agent_kwargs={"mode": "mask"}, concurrency=1,
                      history_minutes=HISTORY_MINUTES, live_traffic=False)
    summary = run_sweep(cfg, progress=None)
    assert summary["outcomes"]["masked"] == 1 and summary["success"] == 0
    md = (out / "report.md").read_text()
    assert "Failed seeds (triage)" in md and "masked" in md
