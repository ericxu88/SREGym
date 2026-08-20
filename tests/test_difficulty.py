"""Difficulty profiles + red herrings: deterministic, additive-only, template-agnostic."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from sregym.harness.agents.scripted import ScriptedAgent
from sregym.harness.episode import EpisodeConfig, run_episode
from sregym.harness.prompts import build_task_prompt
from sregym.scenario import PROFILES, prepare_world
from tests.conftest import FIXED_NOW, HISTORY_MINUTES


def test_profiles():
    assert PROFILES["baseline"].red_herrings == 0 and PROFILES["hard"].red_herrings == 4
    assert PROFILES["hard"].max_steps < PROFILES["standard"].max_steps < PROFILES["baseline"].max_steps


def test_herrings_deterministic_and_additive(tmp_path: Path):
    a, spec_a = prepare_world(seed=9, fault="env_var_typo", root=tmp_path / "a", now=FIXED_NOW,
                              history_minutes=HISTORY_MINUTES, difficulty="hard")
    b, _ = prepare_world(seed=9, fault="env_var_typo", root=tmp_path / "b", now=FIXED_NOW,
                         history_minutes=HISTORY_MINUTES, difficulty="hard")
    base, spec_base = prepare_world(seed=9, fault="env_var_typo", root=tmp_path / "c", now=FIXED_NOW,
                                    history_minutes=HISTORY_MINUTES, difficulty="baseline")
    try:
        assert a.extra["herrings"] == b.extra["herrings"] and len(a.extra["herrings"]) == 4
        assert base.extra.get("herrings", []) == []
        # additive only: the real evidence is identical -- the fault commit, its message and the incident shape
        assert spec_a.incident.root_cause_summary.split(" ", 2)[2:] and spec_a.incident.failing_endpoints == spec_base.incident.failing_endpoints
        assert a.extra["fault_params"]["target"] == base.extra["fault_params"]["target"]
        # the real fix works identically: same spec check types
        assert [c.type for c in spec_a.root_cause_checks] == [c.type for c in spec_base.root_cause_checks]
    finally:
        a.destroy(); b.destroy(); base.destroy()


def test_each_herring_leaves_its_trace(tmp_path: Path):
    world, spec = prepare_world(seed=9, fault="db_file_permissions", root=tmp_path / "w", now=FIXED_NOW,
                                history_minutes=HISTORY_MINUTES, difficulty="hard")
    try:
        h = set(world.extra["herrings"])
        assert h == {"decoy_deploy", "decoy_cron", "bot_scan", "chatter"}
        # decoy deploy: an innocent commit + deferred deploy entry
        assert any(c["message"] == world.extra["herring_decoy_msg"] for c in world.commits)
        assert "restart deferred" in (world.log_dir / "deploy.log").read_text()
        # decoy cron: extra harmless entry, no crond noise about it in the (historical) log
        cron_text = (world.root / "etc/cron.d/checkout-service").read_text()
        assert "OPS-471" in cron_text or "OPS-468" in cron_text
        # bot scan: a 404 burst from one ip in both logs
        log = world.app_log.read_text()
        assert len(re.findall(r"GET /orders/\d{5,} 404", log)) > 50
        ip = world.extra["extra_traffic"][0]["ip"]
        assert (world.root / "var/log/nginx/access.log").read_text().count(ip) > 50
        # chatter: on the page, and it never names the real cause
        page = build_task_prompt(world, spec.incident, fault=spec.fault)
        assert "#incidents" in page
        for forbidden in ("chmod", "permission", "fleetd"):
            assert forbidden not in page.lower()
    finally:
        world.destroy()


def test_scripted_solver_unmoved_by_herrings(tmp_path: Path):
    res = run_episode(ScriptedAgent("solve"),
                      EpisodeConfig(seed=9, fault="db_file_permissions", out_dir=tmp_path / "run", difficulty="hard",
                                    max_steps=PROFILES["hard"].max_steps, history_minutes=HISTORY_MINUTES,
                                    workdir=tmp_path / "w", live_traffic=False))
    assert res.success and res.reward == 1.0
    assert res.difficulty == "hard" and set(res.herrings) == {"decoy_deploy", "decoy_cron", "bot_scan", "chatter"}


def test_cli_difficulty_sets_default_budget(tmp_path: Path):
    import argparse

    from sregym.cli import _resolve_steps

    ns = argparse.Namespace(max_steps=None, difficulty="hard")
    assert _resolve_steps(ns) == PROFILES["hard"].max_steps
    ns = argparse.Namespace(max_steps=25, difficulty="hard")
    assert _resolve_steps(ns) == 25
