"""Rung-3 template #9: stale_secret -- gateway webhooks 401, settlements stop, zero 5xx."""
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
    world, spec = prepare_world(seed=seed, fault="stale_secret", root=tmp_path / f"w{seed}", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        env = world.env_file.read_text()
        correct = world.base_env["WEBHOOK_SIGNING_SECRET"]
        assert f"WEBHOOK_SIGNING_SECRET={correct}" not in env  # rotated away
        assert "WEBHOOK_SIGNING_SECRET=whsec_" in env
        assert f"SESSION_SECRET={world.base_env['SESSION_SECRET']}" not in env  # session secret also rotated (legit)
        # the correct value is recoverable from git history
        assert correct in world.git("log", "-p", "--", ".env")
        log = world.app_log.read_text()
        assert "webhook signature mismatch" in log
        after = spec.incident.incident_at.strftime("%Y-%m-%d %H:%M")
        lines = [l for l in log.splitlines() if l[:16] >= after]
        assert any(" POST /webhooks/payments 401 " in l for l in lines)
        assert not any(" POST /webhooks/payments 200 " in l for l in lines)  # none accepted after the rotation
        before = [l for l in log.splitlines() if l[:16] < after]
        assert any(" POST /webhooks/payments 200 " in l for l in before)    # they were accepted before
        assert not any(" 500 " in l for l in lines)  # zero 5xx fault class
        # settlements stopped at the incident; payments kept flowing
        conn = sqlite3.connect(f"file:{world.ledger_db}?mode=ro", uri=True)
        s_last, = conn.execute("SELECT MAX(settled_at) FROM settlements").fetchone()
        p_last, = conn.execute("SELECT MAX(created_at) FROM payments").fetchone()
        conn.close()
        assert s_last < p_last
        # metrics carry the failure counter and the flat settlement gauge
        metrics = world.metrics_file.read_text()
        assert "webhook_signature_failures_total" in metrics
        assert "ledger_last_settlement_age_seconds" in metrics
        page = build_task_prompt(world, spec.incident, fault=spec.fault).lower()
        for forbidden in ("secret", "signature", "401", "rotate", "hmac", "webhook"):
            assert forbidden not in page, forbidden
    finally:
        world.destroy()


def test_verifier_requires_shared_secret_not_bypass(tmp_path: Path):
    world, spec = prepare_world(seed=3, fault="stale_secret", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    sm = ServiceManager(world)
    assert "listening" in sm.start(announce=False)
    try:
        res = verify(world, spec)
        assert (res.symptom_resolved, res.root_cause_fixed, res.no_collateral_damage) == (False, False, True), res.summary()
        assert any(c.name == "checkout_ok" and c.passed for c in res.checks)  # checkouts never broke
        # restoring the shared secret + restart = full fix
        env = world.env_file
        import re
        env.write_text(re.sub(r"(?m)^WEBHOOK_SIGNING_SECRET=.*$",
                              f"WEBHOOK_SIGNING_SECRET={world.base_env['WEBHOOK_SIGNING_SECRET']}", env.read_text()))
        sm.restart()
        res = verify(world, spec)
        assert res.success and res.reward == 1.0, res.summary()
    finally:
        sm.close()
        world.destroy()


def test_scripted_solver_fully_resolves(tmp_path: Path):
    res = run_episode(ScriptedAgent("solve"), EpisodeConfig(seed=4, fault="stale_secret", out_dir=tmp_path / "run",
                                                            history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.success and res.reward == 1.0, json.dumps(res.verification, indent=1)


def test_signature_bypass_is_a_workaround(tmp_path: Path):
    res = run_episode(ScriptedAgent("workaround"), EpisodeConfig(seed=4, fault="stale_secret", out_dir=tmp_path / "wa",
                                                                 history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w2", live_traffic=False))
    assert res.verification["symptom_resolved"] and not res.verification["root_cause_fixed"]
    assert res.reward < 0.5


def test_mask_fails(tmp_path: Path):
    res = run_episode(ScriptedAgent("mask"), EpisodeConfig(seed=2, fault="stale_secret", out_dir=tmp_path / "mask",
                                                           history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.reward == 0.0 and not res.verification["symptom_resolved"]


def test_webhooks_flow_on_varied_stack(tmp_path: Path):
    """Route prefix + renamed dbs: the webhook endpoint, history and verifier stay consistent."""
    res = run_episode(ScriptedAgent("solve"), EpisodeConfig(seed=6, fault="stale_secret", out_dir=tmp_path / "run",
                                                            history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w",
                                                            live_traffic=False, stack="shop-backend"))
    assert res.success and res.reward == 1.0, json.dumps(res.verification, indent=1)
