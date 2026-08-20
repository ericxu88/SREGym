"""The on-call tool server: one per rollout, owning a freshly generated SREGym world.

`setup_task` (before the MCP endpoint accepts requests) builds the production stack from
the task's seed and starts the live runtime — the faulty service, synthetic traffic, the
metrics collector and cron. The tools are the exact SREGym on-call toolset (paginated
logs, metrics, files, the allow-listed shell, service control) executing in-process
against that world, so the sandbox and its structural forbidden-action judging are
identical to the native harness.

Verification is deterministic and runs *in this process, while the service is live*, at
either terminal moment — the agent calls `resolve_incident`, or the step budget is
exhausted — and the verdict is pushed into the rollout state for the task's reward hook.
"""
from __future__ import annotations

import atexit
import contextlib
import logging
import tempfile
from pathlib import Path
from typing import Any

import verifiers.v1 as vf

logger = logging.getLogger(__name__)


class OnCallState(vf.State):
    world_base: str = ""
    steps_used: int = 0
    done: bool = False          # a verdict exists: resolve_incident ran or the budget is spent
    verdict: dict | None = None  # sregym's 3-part verification result (deterministic)
    steps: list[dict] = []       # tool-call trajectory (feeds the structural forbidden-action check)


class OnCallToolsetConfig(vf.ToolsetConfig):
    keep_world: bool = False
    """Keep the generated world directory after the rollout (debugging)."""
    traffic_rps: float = 1.5
    live_traffic: bool = True


class OnCallToolset(vf.Toolset[OnCallToolsetConfig, OnCallState]):
    TOOL_PREFIX = None  # bare tool names, identical to the native harness

    async def setup_task(self, task) -> None:  # noqa: ANN001 - SREGymTaskData (wire form)
        from sregym import util
        from sregym.harness.episode import LiveWorld
        from sregym.scenario import PROFILES, prepare_world
        from sregym.tools.base import ToolContext, default_registry

        base = Path(tempfile.mkdtemp(prefix=f"sregym-vf-{task.fault}-s{task.seed}-"))
        self.world, self.spec = prepare_world(
            seed=task.seed, fault=task.fault, root=base / "world",
            now=util.parse_iso(task.world_now), history_minutes=task.history_minutes,
            difficulty=task.difficulty, stack=task.stack,
        )
        self.max_steps = int(task.max_steps or PROFILES[task.difficulty].max_steps)
        self.live = LiveWorld(
            self.world, traffic_rps=self.config.traffic_rps, live_traffic=self.config.live_traffic,
            start_service=not bool(self.spec.incident.extra.get("service_dead")),
        )
        self.live.start()
        self.registry = default_registry()
        self.ctx = ToolContext(self.world, self.live.services)
        self._steps: list[dict] = []
        self._verdict: dict | None = None
        self._cleaned = False
        # uvicorn owns the signal handlers; cleanup runs when its graceful shutdown unwinds
        # the server's exit stack (SIGTERM from the launcher's process-group terminate), with
        # atexit as the belt for other exit paths. SIGKILL leaks the temp world dir.
        self._exit_stack.callback(self._cleanup)
        atexit.register(self._cleanup)
        logger.info("world ready: %s (%s, %s)", self.world.base, task.fault, self.world.naming.service)

    # ------------------------------------------------------------------ lifecycle
    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        with contextlib.suppress(Exception):
            self.live.stop()
        # keep the world when no verdict exists and the agent acted: the task's finalize
        # runs the offline fallback verification against it (then deletes it)
        if not self.config.keep_world and (self._verdict is not None or not self._steps):
            with contextlib.suppress(Exception):
                self.world.destroy()

    def _verify_now(self) -> dict:
        """Deterministic 3-part verification against the live world, exactly as the native harness."""
        from sregym.verifier.verify import verify

        result = verify(self.world, self.spec, trajectory_steps=self._steps, base_url=self.world.base_url)
        return {
            "reward": result.reward,
            "success": result.success,
            "symptom_resolved": result.symptom_resolved,
            "root_cause_fixed": result.root_cause_fixed,
            "no_collateral_damage": result.no_collateral_damage,
            "checks": [{"name": c.name, "criterion": c.criterion, "passed": c.passed, "detail": c.detail}
                       for c in result.checks],
            "hidden_root_cause": self.spec.incident.root_cause_summary,
            "steps_used": len(self._steps),
            "max_steps": self.max_steps,
        }

    def _finish(self, verdict: dict) -> None:
        self._verdict = verdict
        self.state.verdict = verdict
        self.state.done = True

    def _call(self, name: str, args: dict[str, Any]) -> str:
        if self._verdict is not None:
            return "error: the incident session has ended (step budget exhausted or already resolved)"
        result = self.registry.call(name, args, self.ctx)
        self._steps.append({
            "step": len(self._steps) + 1, "tool_call": name, "tool_args": args,
            "tool_error": bool(result.is_error),
        })
        self.state.steps = list(self._steps)
        self.state.steps_used = len(self._steps)
        self.state.world_base = str(self.world.base)
        if result.meta.get("terminal"):
            self._finish(self._verify_now())
        elif len(self._steps) >= self.max_steps:
            # budget spent: verify as-is while the world is live (the native harness does the same)
            self._finish(self._verify_now())
        return result.content

    # ------------------------------------------------------------------ tools (the SREGym on-call toolset)
    @vf.tool
    async def read_logs(self, path: str = "", cursor: str = "", limit: int = 0, grep: str = "",
                        since: str = "", until: str = "", tail: bool = False) -> str:
        """Read a log file page by page (max 50 lines per call). Call with no path to list available
        log files. Use grep (regex, case-sensitive; prefix (?i) to ignore case), since/until (UTC
        'HH:MM' or 'YYYY-MM-DD HH:MM:SS') and tail=true to focus; pass the returned next_cursor to
        continue in the same direction with the same filters."""
        args: dict[str, Any] = {}
        if path:
            args["path"] = path
        if cursor:
            args["cursor"] = cursor
        if limit:
            args["limit"] = limit
        if grep:
            args["grep"] = grep
        if since:
            args["since"] = since
        if until:
            args["until"] = until
        if tail:
            args["tail"] = True
        return self._call("read_logs", args)

    @vf.tool
    async def query_metrics(self, metric: str = "", window_minutes: int = 0, until: str = "",
                            step_minutes: int = 0, group_by: str = "", filters: dict[str, str] | None = None) -> str:
        """Query the metrics store (historical + live scrapes). Call with no metric to list metric
        names. Use group_by to split by a label (e.g. status, path) and filters to restrict."""
        args: dict[str, Any] = {}
        if metric:
            args["metric"] = metric
        if window_minutes:
            args["window_minutes"] = window_minutes
        if until:
            args["until"] = until
        if step_minutes:
            args["step_minutes"] = step_minutes
        if group_by:
            args["group_by"] = group_by
        if filters:
            args["filters"] = dict(filters)
        return self._call("query_metrics", args)

    @vf.tool
    async def read_file(self, path: str, start_line: int = 0, max_lines: int = 0) -> str:
        """Read a text file (numbered lines; default first 200). Paths are relative to the host root."""
        args: dict[str, Any] = {"path": path}
        if start_line:
            args["start_line"] = start_line
        if max_lines:
            args["max_lines"] = max_lines
        return self._call("read_file", args)

    @vf.tool
    async def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        """Replace exact text in a file (old_string must match exactly; empty old_string creates the
        file). The service re-reads configuration only on restart."""
        args: dict[str, Any] = {"path": path, "old_string": old_string, "new_string": new_string}
        if replace_all:
            args["replace_all"] = True
        return self._call("edit_file", args)

    @vf.tool
    async def run_shell(self, command: str) -> str:
        """Run a read-mostly shell command in the host root. Allowed: common inspection commands
        (cat, grep, ls, find, head, tail, wc, diff, stat, ps, ...), git (log/show/diff/blame/
        checkout/revert/commit/... no reset/clean/push), sqlite3 (read-only), curl to 127.0.0.1
        only, `python <service>/scripts/<name>.py ...` for the ops scripts that ship with the repo,
        and rm only for files you created yourself. Pipes and ';'/'&&'/'||' are fine; no redirection
        or command substitution."""
        return self._call("run_shell", {"command": command})

    @vf.tool
    async def restart_service(self, action: str = "restart", service: str = "") -> str:
        """Control the managed service process (like systemctl): action=restart (default), status,
        start or stop. Restarting re-reads configuration (.env) and re-imports the application code."""
        args: dict[str, Any] = {"action": action}
        if service:
            args["service"] = service
        return self._call("restart_service", args)

    @vf.tool
    async def resolve_incident(self, summary: str, root_cause: str = "") -> str:
        """Declare the incident resolved and end the session. Provide a short postmortem: what broke,
        the root cause, what you changed, and how you verified recovery. Only call this after you
        have verified the fix."""
        args: dict[str, Any] = {"summary": summary}
        if root_cause:
            args["root_cause"] = root_cause
        return self._call("resolve_incident", args)


if __name__ == "__main__":
    OnCallToolset.run()
