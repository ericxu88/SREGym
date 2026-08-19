"""Rung-3 template #2: unapplied_migration -- schema-ahead-of-code incident, additive schema rule,
db_query checks, code-patch workaround detection, scripted solvability."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from sregym.faults.unapplied_migration import VARIANTS
from sregym.harness.agents.scripted import ScriptedAgent
from sregym.harness.episode import EpisodeConfig, run_episode
from sregym.harness.prompts import build_task_prompt
from sregym.runtime.services import ServiceManager
from sregym.scenario import prepare_world
from sregym.verifier.verify import verify
from tests.conftest import FIXED_NOW, HISTORY_MINUTES


@pytest.fixture
def mig_world(tmp_path: Path):
    world, spec = prepare_world(seed=2, fault="unapplied_migration", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    sm = ServiceManager(world)
    assert "listening" in sm.start(announce=False)
    try:
        yield world, spec, sm
    finally:
        sm.close()
        world.destroy()


def _migrate(world, *args):
    return subprocess.run([world.python, "scripts/migrate.py", *args], cwd=world.repo, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 6, 9])
def test_inject_is_coherent(tmp_path: Path, seed: int):
    world, spec = prepare_world(seed=seed, fault="unapplied_migration", root=tmp_path / f"w{seed}", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        fp = world.extra["fault_params"]
        v = VARIANTS[fp["variant"]]
        # HEAD ships the feature code; the migration file is present iff committed; the db lacks the column either way
        main = (world.repo / "checkout" / "main.py").read_text()
        assert v["columns"][0][0] in main and len(world.commits) == 9
        mig = world.repo / "migrations" / f"{v['migration']}.sql"
        assert mig.exists() == (fp["kind"] == "migration_committed")
        conn = sqlite3.connect(world.core_db)
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({v['table']})")}
        assert v["columns"][0][0] not in cols
        # evidence: only the variant's endpoints fail, with the right error text; health stays 200 in the history
        log = world.app_log.read_text()
        err = next(iter(v["failing"].values()))["error"].split(": ", 1)[1]
        assert err in log and "unable to open database file" not in log and " /health 503 " not in log
        for key in v["failing"]:
            path = key.split(" ", 1)[1].replace("{order_id}", "").replace("{user_id}", "")
            assert re.search(rf"{re.escape(path)}\S* 500 ", log), key
        deploy_log = (world.log_dir / "deploy.log").read_text()
        assert "db migrations: not run by deploy-bot" in deploy_log and spec.incident.deploy_commit[:7] in deploy_log
        page = build_task_prompt(world, spec.incident, fault=spec.fault).lower()
        for forbidden in ("migration", "schema", "column", "deploy", "commit", "migrate"):
            assert forbidden not in page, forbidden
        assert {c.type for c in spec.root_cause_checks} == {"db_query", "files_unchanged"}
    finally:
        world.destroy()


def test_verifier_applied_migration_vs_code_patch(mig_world):
    world, spec, sm = mig_world
    v = VARIANTS[world.extra["fault_params"]["variant"]]
    res = verify(world, spec)
    assert (res.symptom_resolved, res.root_cause_fixed, res.no_collateral_damage) == (False, False, True), res.summary()
    # workaround: patch the code so it stops referencing the column -> symptom ok, root cause not, code changed
    main = world.repo / "checkout" / "main.py"
    src = main.read_text()
    patched = "\n".join(l for l in src.splitlines() if not (l.startswith(("ORDER_COLUMNS +=", "ORDER_LIST_COLUMNS +=", "USER_COLUMNS +=")))) + "\n"
    if v["table"] == "orders" and "coupon_code" in src:
        patched = patched.replace('        columns += ["coupon_code", "discount_cents"]\n        values += [payload.coupon_code, discount]\n', "")
    main.write_text(patched)
    sm.restart()
    res = verify(world, spec)
    assert res.symptom_resolved and not res.root_cause_fixed and not res.no_collateral_damage, res.summary()
    names = {c.name: c.passed for c in res.checks}
    assert names["app_code_unchanged"] is False
    # undo the patch, apply the real migration -> everything passes; schema change is additive, not damage
    main.write_text(src)
    sm.restart()
    if not (world.repo / "migrations" / f"{v['migration']}.sql").exists():
        (world.repo / "migrations" / f"{v['migration']}.sql").write_text(
            "".join(f"ALTER TABLE {v['table']} ADD COLUMN {c} {t};\n" for c, t in v["columns"])
            + f"INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES ('{v['migration']}', 'now');\n")
    proc = _migrate(world, "--apply")
    assert proc.returncode == 0 and "applied" in proc.stdout, proc.stdout + proc.stderr
    res = verify(world, spec)
    assert res.success and res.reward == 1.0, res.summary()
    assert _migrate(world).returncode == 0  # up to date


def test_dropping_a_column_is_still_collateral_damage(mig_world):
    world, spec, sm = mig_world
    # additive changes are fine (tested above); destructive schema changes are not
    conn = sqlite3.connect(world.core_db)
    conn.execute("CREATE TABLE scratch (id INTEGER PRIMARY KEY)")  # additive: fine
    conn.commit()
    assert verify(world, spec).no_collateral_damage
    conn.execute("DROP INDEX idx_orders_user")
    conn.commit()
    res = verify(world, spec)
    assert not res.no_collateral_damage and any("index idx_orders_user dropped" in c.detail for c in res.checks)


@pytest.mark.parametrize("seed", [1, 3])
def test_scripted_solver_fully_resolves(tmp_path: Path, seed: int):
    res = run_episode(ScriptedAgent("solve"), EpisodeConfig(seed=seed, fault="unapplied_migration", out_dir=tmp_path / f"run{seed}",
                                                            history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.success and res.reward == 1.0, json.dumps(res.verification, indent=1)


def test_masking_and_workaround_fail(tmp_path: Path):
    mask = run_episode(ScriptedAgent("mask"), EpisodeConfig(seed=4, fault="unapplied_migration", out_dir=tmp_path / "mask",
                                                            history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert mask.reward == 0.0
    wa = run_episode(ScriptedAgent("workaround"), EpisodeConfig(seed=4, fault="unapplied_migration", out_dir=tmp_path / "wa",
                                                                history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w2", live_traffic=False))
    assert not wa.verification["root_cause_fixed"] and wa.reward < 1.0
