"""Step 2: fault injection produces a coherent incident + spec; the page never names the cause."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from sregym import util
from sregym.faults.base import VerificationSpec, list_faults
from sregym.scenario import prepare_world
from sregym.harness.prompts import build_task_prompt
from tests.conftest import FIXED_NOW, HISTORY_MINUTES


def test_registry_lists_env_var_typo():
    assert "env_var_typo" in list_faults()


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6, 8, 13])
def test_inject_variants_are_coherent(tmp_path: Path, seed: int):
    world, spec = prepare_world(seed=seed, root=tmp_path / f"w{seed}", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        params = world.extra["fault_params"]
        env = util.parse_env_file(world.env_file.read_text())
        target = params["target"]
        # the deployed config is broken in exactly the described way
        if params["kind"] == "key":
            assert target not in env and params["bad_key"] in env
        else:
            assert env[target] == params["bad_value"] and env[target] != world.base_env[target]
        inc = spec.incident
        assert inc.commit_at < inc.deploy_at < inc.restart_at <= inc.incident_at < inc.page_at < inc.support_note_at < world.now
        assert world.commits[-1]["sha"] == inc.deploy_commit and len(world.commits) == 9
        assert world.git("show", "--stat", "--format=", "HEAD").strip().endswith("1 file changed, 2 insertions(+), 2 deletions(-)") \
            or "1 file changed" in world.git("show", "--stat", "--format=", "HEAD")
        assert target in inc.root_cause_summary
        assert {c.type for c in spec.symptom_checks} == {"http"}
        assert [c.type for c in spec.root_cause_checks] == ["env_sqlite_path", "files_unchanged", "path_exists"]
        assert "forbidden_actions" in {c.type for c in spec.collateral_checks}
        # persisted and reloadable
        loaded = VerificationSpec.load(world)
        assert loaded.to_dict() == spec.to_dict()
        # evidence trail reflects the incident
        log = world.app_log.read_text()
        assert inc.error_message in log and f"commit {inc.deploy_commit[:7]}" in log
        for w in inc.config_warnings:
            assert w in log
        deploy_log = (world.log_dir / "deploy.log").read_text()
        assert f"{inc.deploy_at:%Y-%m-%d %H:%M:%S} deploy-bot: [checkout-service] deploy {inc.deploy_commit[:7]} requested by {inc.deploy_author}" in deploy_log
        assert world.extra["history"]["incident_error_rate"] > 0.2
    finally:
        world.destroy()


def test_task_prompt_is_symptom_level_only(tmp_path: Path):
    world, spec = prepare_world(seed=5, root=tmp_path / "w", now=FIXED_NOW, history_minutes=HISTORY_MINUTES)
    try:
        prompt = build_task_prompt(world, spec.incident)
        lower = prompt.lower()
        for forbidden in ("database_url", ".env", "typo", "config", "deploy", "sqlite", "commit", "env var", "unable to open"):
            assert forbidden not in lower, forbidden
        assert "checkout-service" in prompt and "P1" in prompt
        assert re.search(r"\d{2}:\d{2}", prompt)  # timestamped
        assert spec.incident.page_at.strftime("%Y-%m-%d %H:%M:%S") in prompt
    finally:
        world.destroy()
