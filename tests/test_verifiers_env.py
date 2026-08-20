"""The verifiers v1 taskset package (environments/sregym_env): loading, tools, verdicts."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

vf = pytest.importorskip("verifiers.v1")
pytest.importorskip("sregym_env")

from sregym_env.servers.oncall import OnCallToolset, OnCallToolsetConfig  # noqa: E402
from sregym_env.taskset import SREGymConfig, SREGymHarness, SREGymTask, SREGymTaskset  # noqa: E402

from tests.conftest import HISTORY_MINUTES  # noqa: E402


def _first_task(**cfg) -> SREGymTask:
    ts = SREGymTaskset(SREGymConfig(history_minutes=HISTORY_MINUTES, **cfg))
    return next(iter(ts))


def test_loader_resolution_and_harness():
    from verifiers.v1.utils.loaders import default_harness_id, harness_class, taskset_class

    assert taskset_class("sregym-env") is SREGymTaskset
    assert SREGymTaskset.INFINITE
    # the default harness is our null-based tool loop: no bash, no code execution
    assert default_harness_id("sregym-env") == "sregym-env"
    cls = harness_class("sregym-env")
    assert cls is SREGymHarness and cls.SUPPORTS_MCP and not cls.EXECUTES_CODE


def test_task_data_is_portable():
    task = _first_task(faults="env_var_typo")
    d = task.data
    assert task.key == f"env_var_typo-s{d.seed}-baseline-auto"
    assert d.prompt and "PagerDuty" in d.prompt
    assert d.system_prompt and "on-call SRE" in d.system_prompt
    # no host-specific values in the wire data: rollouts rebuild their own world
    assert "/var/" not in d.system_prompt and "/tmp" not in d.system_prompt
    assert d.max_steps == 30 and d.world_now


def test_server_full_resolution(tmp_path: Path):
    task = _first_task(faults="env_var_typo")

    async def run() -> None:
        server = OnCallToolset(OnCallToolsetConfig(live_traffic=False))
        await server.setup_task(task.data)
        try:
            # the rollout world renders the exact page the task shipped
            from sregym.harness.prompts import build_task_prompt

            page = build_task_prompt(server.world, server.spec.incident, fault=server.spec.fault)
            assert page == task.data.prompt
            # fix like the reference solver: restore the broken env line, restart, resolve
            fp = server.world.extra["fault_params"]
            svc = server.world.naming.service
            bad = f"{fp['bad_key']}={fp['bad_value']}"
            good = f"{fp['target']}={server.world.base_env[fp['target']]}"
            out = await server.read_file(path=f"{svc}/.env")
            assert fp["bad_key"] in out
            await server.edit_file(path=f"{svc}/.env", old_string=bad, new_string=good)
            await server.restart_service()
            await server.resolve_incident(summary="restored the env var and restarted")
            st = server.state
            assert st.done and st.verdict["reward"] == 1.0 and st.verdict["success"], st.verdict
            assert st.steps_used == 4 and len(st.steps) == 4
        finally:
            server._cleanup()
            assert not Path(server.world.base).exists()  # verdict computed -> world removed

    asyncio.run(run())


def test_budget_exhaustion_verifies_and_locks(tmp_path: Path):
    task = _first_task(faults="env_var_typo", max_steps=2)
    assert task.data.max_steps == 2

    async def run() -> None:
        server = OnCallToolset(OnCallToolsetConfig(live_traffic=False))
        await server.setup_task(task.data)
        try:
            await server.read_logs()
            assert not server.state.done
            await server.read_logs(tail=True)
            st = server.state
            assert st.done and st.verdict is not None and st.verdict["reward"] == 0.0
            # further calls are refused, not executed
            out = await server.run_shell(command="ls")
            assert "session has ended" in out
            assert st.steps_used == 2
        finally:
            server._cleanup()

    asyncio.run(run())


def test_reward_and_stop_hooks():
    task = _first_task(faults="env_var_typo")
    verdict = {"reward": 0.7, "success": False, "symptom_resolved": False,
               "root_cause_fixed": True, "no_collateral_damage": True}
    trace = SimpleNamespace(state=SimpleNamespace(done=True, verdict=verdict, steps_used=9, steps=[]),
                            info={}, num_turns=3)
    assert asyncio.run(task.incident_closed(trace)) is True
    assert asyncio.run(task.incident_resolution(trace)) == 0.7
    m = asyncio.run(task.components(trace))
    assert m["root_cause_fixed"] == 1.0 and m["symptom_resolved"] == 0.0 and m["steps_used"] == 9.0


def test_finalize_offline_fallback_and_cleanup(tmp_path: Path):
    """Abandonment: no verdict, but the agent acted — finalize verifies the leftover world
    offline (service down => symptom fails; file checks exact) and removes the world dir."""
    from sregym.scenario import prepare_world

    task = _first_task(faults="env_var_typo")
    world, spec = prepare_world(seed=task.data.seed, fault="env_var_typo", root=tmp_path / "w",
                                history_minutes=HISTORY_MINUTES)
    state = SimpleNamespace(verdict=None, world_base=str(world.base), steps_used=1,
                            steps=[{"step": 1, "tool_call": "read_logs", "tool_args": {}, "tool_error": False}])
    trace = SimpleNamespace(state=state, info={})
    asyncio.run(task.finalize(trace, None))
    v = trace.info["verdict"]
    assert v["abandoned"] and not v["symptom_resolved"] and v["reward"] in (0.0, 0.35)
    assert trace.info["sregym"]["fault"] == "env_var_typo"
    assert not (tmp_path / "w").exists()
