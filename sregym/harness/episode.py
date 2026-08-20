"""Episode driver: generate world -> inject fault -> run agent loop -> verify -> emit trajectory."""
from __future__ import annotations

import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sregym import util
from sregym.generator.world import World
from sregym.harness.agents.base import AgentAdapter, ToolCall
from sregym.harness.prompts import build_system_prompt, build_task_prompt
from sregym.harness.trajectory import Step, TrajectoryWriter
from sregym.runtime.cron import CronRunner
from sregym.runtime.metrics import MetricsCollector
from sregym.runtime.services import ServiceManager
from sregym.runtime.traffic import TrafficGenerator
from sregym.scenario import prepare_world  # noqa: F401  (re-exported for callers)
from sregym.tools.base import ToolContext, ToolRegistry, ToolResult, default_registry
from sregym.verifier.verify import VerificationResult, verify


@dataclass
class EpisodeConfig:
    seed: int
    fault: str = "env_var_typo"
    max_steps: int = 30
    token_budget: int = 400_000
    workdir: Path | None = None  # parent dir for the world (default: system temp)
    keep_world: bool = False
    history_minutes: int = 180
    traffic_rps: float = 1.5
    out_dir: Path | None = None  # where trajectory/result files go
    live_traffic: bool = True
    now: datetime | None = None
    prompt_style: str = "full"  # see harness.prompts.PROMPT_STYLES
    difficulty: str = "baseline"  # see scenario.PROFILES (red herrings; also the default step budget)
    stack: str = "auto"  # stack identity: auto (seeded), classic, or a variant service name


@dataclass
class EpisodeResult:
    seed: int
    fault: str
    reward: float
    success: bool
    verification: dict[str, Any]
    stop_reason: str
    steps: int
    usage: dict[str, int]
    trajectory_path: str
    world_root: str
    agent: dict[str, Any] = field(default_factory=dict)
    agent_summary: str = ""
    hidden_root_cause: str = ""
    fault_params: dict[str, Any] = field(default_factory=dict)  # e.g. target/kind/innocent change (for per-variant reports)
    difficulty: str = "baseline"
    herrings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    error: str | None = None
    infra_error: str | None = None  # environment problem (service failed to start, API outage): result is not a model outcome

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- live runtime
class LiveWorld:
    """Runs the service, synthetic traffic and the metrics collector for a world."""

    def __init__(self, world: World, traffic_rps: float = 1.5, live_traffic: bool = True, start_service: bool = True):
        self.world = world
        self.start_service = start_service  # False for incidents where the service is down/crash-looping at page time
        self.services = ServiceManager(world)
        self.traffic = TrafficGenerator(world, rps=traffic_rps) if live_traffic else None
        self.collector = MetricsCollector(world)
        self.cron = CronRunner(world)  # runs etc/cron.d jobs (deployed repo scripts only)

    def start(self) -> str:
        if self.start_service:
            msg = self.services.start(announce=False)  # in the fiction the process has been up since the deploy
        else:
            msg = f"{self.world.base_url} is down (the incident took the service out; traffic is getting 502s)"
        self.collector.start()
        self.cron.start()
        if self.traffic:
            self.traffic.start()
        return msg

    def stop(self) -> None:
        if self.traffic:
            self.traffic.stop()
        self.cron.stop()
        self.collector.stop()
        self.services.close()

    def __enter__(self) -> "LiveWorld":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


# --------------------------------------------------------------------------- episode loop
def run_episode(agent: AgentAdapter, config: EpisodeConfig, registry: ToolRegistry | None = None,
                verbose: bool = False) -> EpisodeResult:
    started = time.time()
    registry = registry or default_registry()
    root = None
    if config.workdir:
        root = Path(config.workdir) / f"world-seed{config.seed}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S%f}"
    world, spec = prepare_world(config.seed, config.fault, root=root, now=config.now,
                                history_minutes=config.history_minutes, difficulty=config.difficulty,
                                stack=config.stack)
    out_dir = Path(config.out_dir) if config.out_dir else Path("runs") / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-seed{config.seed}-{agent.name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    traj_path = out_dir / "trajectory.jsonl"
    system_prompt = build_system_prompt(world, config.max_steps, style=config.prompt_style)
    task_prompt = build_task_prompt(world, spec.incident, fault=spec.fault)
    (out_dir / "prompt.txt").write_text(system_prompt + "\n\n---\n\n" + task_prompt + "\n")

    writer = TrajectoryWriter(traj_path)
    writer.write_meta(
        seed=config.seed, fault=config.fault, world_root=str(world.root), world_base=str(world.base), port=world.port, agent=agent.describe(),
        fault_params=dict(world.extra.get("fault_params", {})), difficulty=config.difficulty,
        herrings=list(world.extra.get("herrings", [])),
        max_steps=config.max_steps, token_budget=config.token_budget, prompt_style=config.prompt_style,
        system_prompt=system_prompt, task_prompt=task_prompt,
        incident=spec.incident.to_dict(), spec=spec.to_dict(), started_at=util.fmt_iso(datetime.now(timezone.utc)),
    )
    service_dead = bool(spec.incident.extra.get("service_dead"))
    live = LiveWorld(world, traffic_rps=config.traffic_rps, live_traffic=config.live_traffic,
                     start_service=not service_dead)
    ctx = ToolContext(world, live.services)
    steps: list[Step] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    infra_error: str | None = None
    stop_reason = "unknown"
    error: str | None = None
    agent_summary = ""
    if verbose:
        print(f"[sregym] world at {world.base} (host root {world.root}, port {world.port}); fault={config.fault} seed={config.seed}")
        print(f"[sregym] hidden root cause: {spec.incident.root_cause_summary}")
    try:
        start_msg = live.start()
        if not service_dead and "listening" not in start_msg:
            infra_error = f"service did not start: {start_msg}"
            raise RuntimeError(infra_error)
        agent.bind_world(world)
        agent.start(system_prompt, task_prompt, registry.specs(ctx))
        observation = task_prompt
        step_no = 0
        nudged = False
        while True:
            turn = agent.next_turn()
            for k in usage_total:
                usage_total[k] += int(turn.usage.get(k, 0) or 0)
            if verbose and turn.text:
                print(f"[agent] {turn.text.strip()[:600]}")
            if not turn.tool_calls:
                if turn.stop or nudged:
                    stop_reason = "agent_stopped"
                    break
                nudged = True
                agent.nudge("No tool call received. If the incident is fully resolved and verified, call resolve_incident; "
                            "otherwise continue investigating with the tools.")
                continue
            results: list[tuple[ToolCall, ToolResult]] = []
            for call in turn.tool_calls:
                step_no += 1
                result = registry.call(call.name, call.args, ctx)
                state_hash = world.state_hash(live.services.pid)
                # observation = what the model had seen when it chose this call (same for every call in one turn)
                step = Step(step=step_no, observation=observation, assistant_text=turn.text, tool_call=call.name,
                            tool_args=call.args, tool_result=result.content, tool_error=result.is_error,
                            state_hash=state_hash, usage=turn.usage if call is turn.tool_calls[0] else None,
                            assistant_thinking=turn.thinking if call is turn.tool_calls[0] else None)
                steps.append(step)
                writer.write_step(step)
                if verbose:
                    print(f"[step {step_no}] {call.name}({_short_args(call.args)}) -> {'ERROR ' if result.is_error else ''}{result.content[:300].strip()!r}")
                results.append((call, result))
                if result.meta.get("terminal"):
                    stop_reason = "resolved"
                    agent_summary = str(result.meta.get("summary", ""))
                    break
                if step_no >= config.max_steps:
                    stop_reason = "max_steps"
                    break
            if stop_reason != "unknown":
                break
            agent.observe(results)
            observation = results[0][1].content if len(results) == 1 else \
                "\n\n".join(f"[{c.name}] {r.content}" for c, r in results)
            if usage_total["input_tokens"] + usage_total["output_tokens"] > config.token_budget:
                stop_reason = "token_budget"
                break
    except KeyboardInterrupt:
        live.stop()
        writer.write_end(stop_reason="interrupted", reward=0.0, success=False, steps=len(steps), usage=usage_total)
        writer.close()
        raise
    except Exception as e:  # noqa: BLE001 - record and still verify
        stop_reason = "infra_error" if infra_error else "agent_error"
        error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        if not infra_error and _looks_like_infra_error(e):
            infra_error = f"{type(e).__name__}: {str(e)[:300]}"
        if verbose:
            print(f"[sregym] agent error: {error}")
    # ------------------------------------------------------------------ verify against the live world
    try:
        verification = verify(world, spec, world.load_manifest(), [s.to_record() for s in steps])
    except Exception as e:  # noqa: BLE001
        verification = VerificationResult(False, False, False, 0.0, False, [])
        error = (error or "") + f"\nverification crashed: {e}"
    finally:
        live.stop()
    result = EpisodeResult(
        seed=config.seed, fault=config.fault, reward=verification.reward, success=verification.success,
        verification=verification.to_dict(), stop_reason=stop_reason, steps=len(steps), usage=usage_total,
        trajectory_path=str(traj_path), world_root=str(world.base), agent=agent.describe(), agent_summary=agent_summary,
        hidden_root_cause=spec.incident.root_cause_summary, fault_params=dict(world.extra.get("fault_params", {})),
        difficulty=config.difficulty, herrings=list(world.extra.get("herrings", [])),
        duration_s=round(time.time() - started, 2), error=error, infra_error=infra_error,
    )
    writer.write_end(stop_reason=stop_reason, reward=verification.reward, success=verification.success,
                     verification=verification.to_dict(), usage=usage_total, steps=len(steps), agent_summary=agent_summary,
                     hidden_root_cause=spec.incident.root_cause_summary, error=error, infra_error=infra_error,
                     fault_params=dict(world.extra.get("fault_params", {})),
                     ended_at=util.fmt_iso(datetime.now(timezone.utc)))
    writer.close()
    util.write_json(out_dir / "result.json", result.to_dict())
    if verbose:
        print(verification.summary())
    if not config.keep_world:
        world.destroy()
    return result


def _looks_like_infra_error(exc: BaseException) -> bool:
    """API/network failures that say nothing about the model's ability (retryable at the sweep level)."""
    name = type(exc).__name__
    if name in ("RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError", "OverloadedError",
                "APIStatusError", "ServiceUnavailableError", "AuthenticationError", "PermissionDeniedError"):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _short_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{k}={s!r}")
    return ", ".join(parts)
