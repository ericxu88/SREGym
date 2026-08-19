"""Rung-3 template #3: cron_write_lock -- intermittent lock contention, live cron runner,
probe-window verification, scripted solvability."""
from __future__ import annotations

import collections
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sregym.harness.agents.scripted import ScriptedAgent
from sregym.harness.episode import EpisodeConfig, run_episode
from sregym.harness.prompts import build_task_prompt
from sregym.runtime.cron import CronRunner, field_matches, parse_crontab, schedule_matches
from sregym.scenario import prepare_world
from tests.conftest import FIXED_NOW, HISTORY_MINUTES


def test_cron_schedule_matching():
    now = datetime(2026, 8, 19, 14, 32, tzinfo=timezone.utc)
    assert schedule_matches(["*", "*", "*", "*", "*"], now)
    assert schedule_matches(["*/2", "*", "*", "*", "*"], now) and not schedule_matches(["*/5", "*", "*", "*", "*"], now)
    assert schedule_matches(["32", "14", "*", "*", "*"], now) and not schedule_matches(["30", "3", "*", "*", "*"], now)
    assert schedule_matches(["*/15", "*", "*", "*", "*"], now.replace(minute=45))
    assert field_matches("10-20", 15, 0, 59) and not field_matches("10-20", 25, 0, 59)
    assert field_matches("1,3,5", 3, 0, 59) and not field_matches("1,3,5", 2, 0, 59)


@pytest.mark.parametrize("seed", [1, 4, 7, 12])
def test_inject_is_coherent_and_intermittent(tmp_path: Path, seed: int):
    world, spec = prepare_world(seed=seed, fault="cron_write_lock", root=tmp_path / f"w{seed}", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        cron_file = world.root / "etc/cron.d/checkout-service"
        jobs = [j for j in parse_crontab(cron_file.read_text()) if "archive_orders" in j["command"]]
        assert len(jobs) == 1 and jobs[0]["schedule"] == ["*", "*", "*", "*", "*"]
        assert cron_file.stat().st_mtime < FIXED_NOW.timestamp()  # backdated to the reload
        # bursty and intermittent: locked errors cluster in the first half of each minute; successes interleave
        log = world.app_log.read_text()
        locked = [l for l in log.splitlines() if "database is locked" in l and "checkout.access" in l]
        assert locked and all(" POST /checkout 500 5" in l for l in locked)  # ~5s busy timeout latency
        buckets = collections.Counter(int(l[17:19]) // 15 for l in locked)
        assert buckets.get(3, 0) == 0 and buckets.get(0, 0) > 0  # nothing in the last quarter of the minute
        after = spec.incident.incident_at.strftime("%Y-%m-%d %H:%M")
        ok_checkouts = [l for l in log.splitlines() if " POST /checkout 201 " in l and l[:16] >= after]
        assert ok_checkouts, "successful checkouts continue between bursts"
        # no deploy, no restart in the incident window
        assert "starting checkout-service" not in log[log.index(after.replace(" ", " ")):] if after in log else True
        deploy_log = (world.log_dir / "deploy.log").read_text()
        assert after[:10] not in deploy_log or spec.incident.deploy_commit[:7] not in deploy_log.split("\n")[-3]
        cron_log = (world.log_dir / "cron.log").read_text()
        assert "archive_orders: scanned" in cron_log and "transaction held" in cron_log and "RELOAD" in cron_log
        page = build_task_prompt(world, spec.incident, fault=spec.fault).lower()
        for forbidden in ("cron", "archive", "lock", "job", "schedule"):
            assert forbidden not in page, forbidden
        assert "checkout" in page and re.search(r"\d{2}:\d{2}", page)
    finally:
        world.destroy()


def test_cron_runner_only_runs_deployed_repo_scripts(tmp_path: Path):
    world, spec = prepare_world(seed=2, fault="cron_write_lock", root=tmp_path / "w", history_minutes=HISTORY_MINUTES)
    try:
        runner = CronRunner(world)
        v = runner._validate(f"cd {world.repo} && {world.python} scripts/expire_carts.py >> logs/cron.log 2>&1")
        assert v == ["python", "scripts/expire_carts.py"]
        assert runner._validate(f"cd {world.repo} && {world.python} scripts/archive_orders.py --retention-days 365") == \
            ["python", "scripts/archive_orders.py", "--retention-days", "365"]
        for bad in ["sqlite3 data/checkout.db 'PRAGMA optimize;'", f"cd /tmp && {world.python} scripts/x.py",
                    f"cd {world.repo} && {world.python} checkout/serve.py", f"cd {world.repo} && {world.python} scripts/x.py; rm -rf /",
                    f"cd {world.repo} && {world.python} scripts/x.py --flag `id`"]:
            assert runner._validate(bad) is None, bad
        # a modified script must be skipped (and say so in cron.log)
        script = world.repo / "scripts" / "expire_carts.py"
        script.write_text(script.read_text() + "\nprint('tampered')\n")
        job = parse_crontab((world.root / "etc/cron.d/checkout-service").read_text())[0]
        runner._run_job(job, datetime.now(timezone.utc))
        assert "skipped scripts/expire_carts.py (not the deployed version" in (world.log_dir / "cron.log").read_text()
    finally:
        world.destroy()


@pytest.mark.timeout(600)
def test_live_episode_solve_and_probe_window(tmp_path: Path):
    """One live episode: real cron job causes real lock errors; scripted fix passes the probe window."""
    res = run_episode(ScriptedAgent("solve"), EpisodeConfig(seed=4, fault="cron_write_lock", out_dir=tmp_path / "run",
                                                            history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.success and res.reward == 1.0, json.dumps(res.verification, indent=1)
    names = {c["name"]: c for c in res.verification["checks"]}
    assert "probes of POST /checkout succeeded" in names["checkouts_stay_up_for_a_window"]["detail"]


@pytest.mark.timeout(600)
def test_mask_fails_probe_window(tmp_path: Path):
    """Restarting the service does not stop the cron job: the probe window must catch the next burst."""
    res = run_episode(ScriptedAgent("mask"), EpisodeConfig(seed=5, fault="cron_write_lock", out_dir=tmp_path / "mask",
                                                           history_minutes=HISTORY_MINUTES, workdir=tmp_path / "w", live_traffic=False))
    assert res.reward == 0.0 and not res.verification["symptom_resolved"]
    assert not res.verification["root_cause_fixed"]
