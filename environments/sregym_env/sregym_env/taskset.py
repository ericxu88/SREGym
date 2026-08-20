"""SREGym as a verifiers v1 taskset: infinite, verifiable, un-memorizable on-call incidents.

Every task is one procedurally generated production incident: a seed deterministically
builds a complete stack (FastAPI service + SQLite + git history + nginx/systemd/cron
config + hours of coherent logs and metrics), draws one of several stack identities so
"the playbook" does not transfer between episodes, and injects a known fault from the
template library. The agent investigates through SREGym's sandboxed on-call toolset (an
MCP server owning the live world, one per rollout) and is scored by SREGym's
deterministic 3-part verifier — symptom resolved, true root cause fixed, no collateral
damage — with no LLM judge anywhere.

The taskset is INFINITE: seeds are free, so bound a run with `-n`. Rollout r>1 of one
task replays the same incident content (same seed, same fault, same stack identity, same
history) in an isolated world copy on its own port.
"""
from __future__ import annotations

import itertools
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import verifiers.v1 as vf
from verifiers.v1.harnesses.null import NullHarness

from sregym_env.servers.oncall import OnCallState, OnCallToolset, OnCallToolsetConfig

# faults included under faults="all" (kept explicit so config surface is stable)
ALL_FAULTS = [
    "env_var_typo", "ledger_divergence", "unapplied_migration", "cron_write_lock",
    "db_file_permissions", "bad_dependency_pin", "rate_limit_misconfig",
    "disk_full", "stale_secret", "truncated_env",
]


class SREGymTaskData(vf.TaskData):
    seed: int
    fault: str
    difficulty: str = "baseline"
    stack: str = "auto"
    max_steps: int = 0            # 0 -> the difficulty profile's budget
    history_minutes: int = 180
    world_now: str = ""           # ISO; the world's generation-time "now" (load-time wall clock)


class SREGymTaskConfig(vf.TaskConfig):
    tools: OnCallToolsetConfig = OnCallToolsetConfig()


class SREGymTask(vf.Task[SREGymTaskData, OnCallState, SREGymTaskConfig]):
    @classmethod
    def toolsets(cls, config: SREGymTaskConfig) -> list[vf.Toolset]:
        return [OnCallToolset(config.tools)]

    @property
    def key(self) -> str:
        # durable identity: content varies with world_now, the incident does not
        d = self.data
        return f"{d.fault}-s{d.seed}-{d.difficulty}-{d.stack}"

    # ------------------------------------------------------------------ stops
    @vf.stop
    async def incident_closed(self, trace: vf.Trace) -> bool:
        """The tool server verified the world: resolve_incident ran or the step budget is spent."""
        return bool(trace.state.done)

    @vf.stop
    async def turn_backstop(self, trace: vf.Trace) -> bool:
        # a chatty model that stops calling tools still terminates
        budget = self.data.max_steps or 30
        return trace.num_turns >= 2 * budget + 10

    # ------------------------------------------------------------------ scoring
    async def finalize(self, trace: vf.Trace, runtime) -> None:  # noqa: ANN001
        """Abandonment fallback: the model quit without resolve_incident and with budget left, so
        the tool server never verified. Score the world as it was left — the service is down (an
        abandoned incident is not a restored service), file/db/git/forbidden checks are exact.

        Also persists a compact verdict into ``trace.info`` (rollout state is not serialized
        into traces; ``info`` is), so runs stay debuggable post-hoc."""
        state = trace.state
        if state.verdict is None and state.world_base:
            from sregym.generator.world import World
            from sregym.verifier.verify import verify

            world = World.load(Path(state.world_base))
            result = verify(world, self.spec_for(world), trajectory_steps=list(state.steps))
            trace.info["verdict"] = {
                "reward": result.reward, "success": result.success,
                "symptom_resolved": result.symptom_resolved,
                "root_cause_fixed": result.root_cause_fixed,
                "no_collateral_damage": result.no_collateral_damage,
                "abandoned": True, "steps_used": state.steps_used,
            }
        if state.world_base and not self.config.tools.keep_world:
            shutil.rmtree(state.world_base, ignore_errors=True)
        v = state.verdict or trace.info.get("verdict")
        if v is not None:
            failed = [f"{c['criterion']}:{c['name']}" for c in v.get("checks", []) if not c["passed"]]
            trace.info["sregym"] = {
                "fault": self.data.fault, "seed": self.data.seed, "difficulty": self.data.difficulty,
                "stack": self.data.stack, "reward": v.get("reward"), "success": v.get("success"),
                "symptom_resolved": v.get("symptom_resolved"), "root_cause_fixed": v.get("root_cause_fixed"),
                "no_collateral_damage": v.get("no_collateral_damage"), "steps_used": state.steps_used,
                "failed_checks": failed, "hidden_root_cause": v.get("hidden_root_cause"),
                "steps": [{"tool": st.get("tool_call"), "error": bool(st.get("tool_error"))} for st in state.steps],
            }

    @staticmethod
    def spec_for(world):  # noqa: ANN001, ANN205
        from sregym.faults.base import VerificationSpec

        return VerificationSpec.load(world)

    def _verdict(self, trace: vf.Trace) -> dict | None:
        return trace.state.verdict or trace.info.get("verdict")

    @vf.reward
    async def incident_resolution(self, trace: vf.Trace) -> float:
        """SREGym's deterministic reward: 1.0 iff symptom resolved AND root cause fixed AND no
        collateral damage; else 0.3*symptom + 0.7*root_cause, halved on collateral damage."""
        v = self._verdict(trace)
        return float(v["reward"]) if v else 0.0

    @vf.metric
    async def components(self, trace: vf.Trace) -> dict[str, float]:
        v = self._verdict(trace) or {}
        return {
            "symptom_resolved": float(bool(v.get("symptom_resolved"))),
            "root_cause_fixed": float(bool(v.get("root_cause_fixed"))),
            "no_collateral_damage": float(bool(v.get("no_collateral_damage"))),
            "steps_used": float(trace.state.steps_used),
            "abandoned": float(bool(v.get("abandoned"))),
        }


class SREGymConfig(vf.TasksetConfig):
    faults: str = "all"
    """Comma-separated fault template names, or "all". Also accepts composed pairs, e.g.
    "composed:migration+perms"."""
    difficulty: str = "baseline"
    """baseline | standard | hard — step budget (30/20/12) + seeded red herrings (0/2/4)."""
    stack: str = "auto"
    """auto = seeded per-world stack identity; classic = the original checkout-service; or a
    specific variant service name."""
    seed_start: int = 1
    max_steps: int = 0
    """Explicit step budget; 0 uses the difficulty profile's."""
    history_minutes: int = 180
    task: SREGymTaskConfig = SREGymTaskConfig()


class SREGymTaskset(vf.Taskset[SREGymTask, SREGymConfig]):
    INFINITE = True  # seeds are free; bound a run with -n

    def load(self) -> Iterator[SREGymTask]:
        from sregym import util
        from sregym.faults.base import get_fault, list_faults

        faults = ALL_FAULTS if self.config.faults == "all" else [f.strip() for f in self.config.faults.split(",") if f.strip()]
        known = set(list_faults())
        for f in faults:
            if not (f in known or f.startswith("composed")):
                raise ValueError(f"unknown fault {f!r}; known: {sorted(known)}")
            get_fault(f)  # composed pair validation
        now = util.utcnow()
        for i in itertools.count():
            seed = self.config.seed_start + i
            fault = faults[i % len(faults)]
            yield self._make_task(seed, fault, now)

    def _make_task(self, seed: int, fault: str, now) -> SREGymTask:  # noqa: ANN001
        """Build the world once, render its page + portable system prompt, discard the world.
        The per-rollout tool server rebuilds an identical world from (seed, fault, now, ...)."""
        from sregym import util
        from sregym.harness.prompts import build_portable_system_prompt, build_task_prompt
        from sregym.scenario import PROFILES, prepare_world

        cfg = self.config
        base = Path(tempfile.mkdtemp(prefix="sregym-load-"))
        try:
            world, spec = prepare_world(seed=seed, fault=fault, root=base / "w", now=now,
                                        history_minutes=cfg.history_minutes,
                                        difficulty=cfg.difficulty, stack=cfg.stack)
            max_steps = cfg.max_steps or PROFILES[cfg.difficulty].max_steps
            page = build_task_prompt(world, spec.incident, fault=spec.fault)
            system = build_portable_system_prompt(world, max_steps)
            data = SREGymTaskData(
                idx=seed, seed=seed, fault=fault, difficulty=cfg.difficulty, stack=cfg.stack,
                max_steps=max_steps, history_minutes=cfg.history_minutes,
                world_now=util.fmt_iso(world.now),
                name=f"{fault} (seed {seed})",
                description=f"On-call incident: {fault} on {world.naming.service} ({cfg.difficulty})",
                prompt=page, system_prompt=system,
            )
            return SREGymTask(data, cfg.task)
        finally:
            shutil.rmtree(base, ignore_errors=True)


class SREGymHarness(NullHarness):
    """The taskset's default harness: a pure chat tool-loop. The model gets exactly SREGym's
    sandboxed MCP toolset — no bash tool, no host filesystem, no code execution outside the
    environment's own tools. (Choosing a shell-bearing harness like `bash` would bypass the
    sandbox that SREGym's structural forbidden-action judging is built on.)"""


__all__ = ["SREGymTaskset", "SREGymHarness"]
