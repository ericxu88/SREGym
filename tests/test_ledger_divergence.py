"""Rung-3 template #1: ledger_divergence -- silent data divergence, data-divergence verifier checks,
hash-pinned repo script execution, scripted solvability."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from sregym import util
from sregym.harness.agents.scripted import ScriptedAgent
from sregym.harness.episode import EpisodeConfig, run_episode
from sregym.harness.prompts import build_task_prompt
from sregym.runtime.services import ServiceManager
from sregym.scenario import prepare_world
from sregym.tools.base import ToolContext, default_registry
from tests.conftest import FIXED_NOW, HISTORY_MINUTES


@pytest.fixture
def ledger_world(tmp_path: Path):
    world, spec = prepare_world(seed=3, fault="ledger_divergence", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    sm = ServiceManager(world)
    assert "listening" in sm.start(announce=False)
    try:
        yield world, spec, sm
    finally:
        sm.close()
        world.destroy()


def _fix_env(world):
    snap = world.extra["fault_params"]["snapshot"]
    text = world.env_file.read_text().replace(f"LEDGER_DATABASE_URL=sqlite:///{snap}", "LEDGER_DATABASE_URL=sqlite:///data/ledger.db")
    world.env_file.write_text(text)


def _reconcile(world, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([world.python, "scripts/reconcile_ledger.py", *args], cwd=world.repo, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})


@pytest.mark.parametrize("seed", [1, 2, 3, 5, 8])
def test_inject_is_coherent_and_silent(tmp_path: Path, seed: int):
    world, spec = prepare_world(seed=seed, fault="ledger_divergence", root=tmp_path / f"w{seed}", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        fp = world.extra["fault_params"]
        env = util.parse_env_file(world.env_file.read_text())
        assert env["LEDGER_DATABASE_URL"] == f"sqlite:///{fp['snapshot']}" and (world.repo / fp["snapshot"]).exists()
        # snapshot is a strict subset of the ledger as of its date, plus the diverted payments
        snap = sqlite3.connect(world.repo / fp["snapshot"])
        diverted = snap.execute("SELECT COUNT(*) FROM payments WHERE created_at >= ?", (util.fmt_iso(spec.incident.incident_at),)).fetchone()[0]
        assert diverted == world.extra["history"]["diverted_payments"] > 0
        ledger = sqlite3.connect(world.ledger_db)
        assert ledger.execute("SELECT COUNT(*) FROM payments WHERE created_at >= ?", (util.fmt_iso(spec.incident.incident_at),)).fetchone()[0] == 0
        # no errors anywhere: the incident is silent
        log = world.app_log.read_text()
        assert " 500 " not in log and "Traceback" not in log and f"commit {spec.incident.deploy_commit[:7]}" in log
        deploy_log = (world.log_dir / "deploy.log").read_text()
        if fp["lagged"]:
            assert "restart deferred" in deploy_log and len(world.commits) == 10
            assert world.git("log", "-1", "--format=%s").strip() != spec.incident.root_cause_summary  # HEAD is the innocent release
        else:
            assert "restart deferred" not in deploy_log and len(world.commits) == 9
        # metrics carry the finance-side signal
        rows = [json.loads(l) for l in world.metrics_file.read_text().splitlines()]
        ages = [r["v"] for r in rows if r["m"] == "ledger_last_payment_age_seconds"]
        assert ages and ages[-1] > 300 and min(ages[:5]) < 300
        # page never names the cause
        page = build_task_prompt(world, spec.incident, fault=spec.fault).lower()
        for forbidden in ("snapshot", "ledger_database_url", ".env", "config", "deploy", "commit", "restart"):
            assert forbidden not in page, forbidden
        assert "ledger" in page and re.search(r"\d{2}:\d{2}", page)
        assert {c.type for c in spec.symptom_checks} == {"http", "ledger_complete"}
    finally:
        world.destroy()


def test_verifier_requires_config_restart_and_backfill(ledger_world):
    from sregym.verifier.verify import verify

    world, spec, sm = ledger_world
    res = verify(world, spec)
    assert (res.symptom_resolved, res.root_cause_fixed, res.no_collateral_damage) == (False, False, True), res.summary()
    names = {c.name: c for c in res.checks}
    assert not names["checkout_payment_in_ledger"].passed and not names["ledger_complete_since_incident"].passed
    # config fixed + restarted, but the diverted payments are still missing -> symptom not resolved (0.7)
    _fix_env(world)
    sm.restart()
    res = verify(world, spec)
    names = {c.name: c for c in res.checks}
    assert names["checkout_payment_in_ledger"].passed and not names["ledger_complete_since_incident"].passed
    assert (res.symptom_resolved, res.root_cause_fixed, res.no_collateral_damage) == (False, True, True) and res.reward == 0.7
    # backfill with the shipped script -> fully resolved
    proc = _reconcile(world, "--source", world.extra["fault_params"]["snapshot"], "--apply")
    assert proc.returncode == 0 and "copied" in proc.stdout, proc.stdout + proc.stderr
    res = verify(world, spec)
    assert res.success and res.reward == 1.0, res.summary()
    # and the reconcile report agrees
    assert "missing: 0" in _reconcile(world).stdout


def test_repo_scripts_run_only_when_unmodified(ledger_world):
    world, spec, sm = ledger_world
    ctx = ToolContext(world, sm)
    reg = default_registry()
    r = reg.call("run_shell", {"command": "python checkout-service/scripts/reconcile_ledger.py --since 00:00"}, ctx)
    assert not r.is_error or "missing:" in r.content  # exit code 2 = missing payments reported (still ran)
    assert "reconcile: core=data/checkout.db target=" in r.content
    # repo-relative form and root-relative path args both work
    snap = world.extra["fault_params"]["snapshot"]
    r = reg.call("run_shell", {"command": f"python scripts/reconcile_ledger.py --source checkout-service/{snap}"}, ctx)
    assert "source" in r.content and "has payments for" in r.content
    for bad in ["python -c 'print(1)'", "python3 -m http.server", "python checkout-service/checkout/serve.py",
                "python checkout-service/scripts/nope.py", "python /etc/passwd"]:
        r = reg.call("run_shell", {"command": bad}, ctx)
        assert r.is_error, (bad, r.content)
    # a modified script is refused (hash pinned to the generation-time manifest)
    script = world.repo / "scripts" / "reconcile_ledger.py"
    script.write_text(script.read_text() + "\nprint('tampered')\n")
    r = reg.call("run_shell", {"command": "python checkout-service/scripts/reconcile_ledger.py"}, ctx)
    assert r.is_error and "modified" in r.content


@pytest.mark.parametrize("seed", [2, 4])
def test_scripted_solver_fully_resolves(tmp_path: Path, seed: int):
    res = run_episode(ScriptedAgent("solve"), EpisodeConfig(seed=seed, fault="ledger_divergence", out_dir=tmp_path / f"run{seed}",
                                                            history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.success and res.reward == 1.0, json.dumps(res.verification, indent=1)
    assert res.fault_params["target"] == "LEDGER_DATABASE_URL"


def test_masking_and_workaround_do_not_pass(tmp_path: Path):
    mask = run_episode(ScriptedAgent("mask"), EpisodeConfig(seed=6, fault="ledger_divergence", out_dir=tmp_path / "mask",
                                                            history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert mask.reward == 0.0
    wa = run_episode(ScriptedAgent("workaround"), EpisodeConfig(seed=6, fault="ledger_divergence", out_dir=tmp_path / "wa",
                                                                history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w2", live_traffic=False))
    assert not wa.verification["root_cause_fixed"] and wa.reward < 1.0
