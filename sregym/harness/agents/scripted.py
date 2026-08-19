"""Deterministic reference agent (no LLM). Useful for smoke tests, demos without an API
key, and as a solvability oracle for generated worlds.

Modes:
  solve       investigate, fix the config from the deploy diff, restart, verify, resolve  (expected reward 1.0)
  mask        just restart the service and declare victory                                (symptom-masked)
  workaround  make the app fall back to the right DB path in code, restart                (root cause not fixed)
  noop        resolve immediately without doing anything
  sloppy      like solve, but also edits an unrelated file (collateral damage)
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Generator

from sregym import util
from sregym.harness.agents.base import AgentAdapter, AgentTurn, ToolCall
from sregym.tools.base import ToolResult

REPO = "checkout-service"


class ScriptedAgent(AgentAdapter):
    name = "scripted"

    def __init__(self, mode: str = "solve"):
        if mode not in ("solve", "mask", "workaround", "noop", "sloppy"):
            raise ValueError(f"unknown scripted mode {mode!r}")
        self.mode = mode
        self._gen: Generator[ToolCall, ToolResult | None, None] | None = None
        self._pending: ToolResult | None = None
        self._n = 0
        self.port: int | None = None
        self.notes: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {"agent": self.name, "mode": self.mode}

    # ------------------------------------------------------------------ adapter API
    def start(self, system_prompt: str, task_prompt: str, tool_specs: list[dict[str, Any]]) -> None:
        m = re.search(r"http://127\.0\.0\.1:(\d+)", system_prompt)
        self.port = int(m.group(1)) if m else None
        self._gen = self._script_ledger() if "ledger" in task_prompt.lower() else self._script()
        self._pending = None

    def next_turn(self) -> AgentTurn:
        assert self._gen is not None
        try:
            call = self._gen.send(self._pending) if self._pending is not None or self._n else next(self._gen)
        except StopIteration:
            return AgentTurn(text="(scripted agent finished)", stop=True)
        self._pending = None
        return AgentTurn(text=self.notes.pop() if self.notes else None, tool_calls=[call], usage={"input_tokens": 0, "output_tokens": 0})

    def observe(self, results: list[tuple[ToolCall, ToolResult]]) -> None:
        self._pending = results[-1][1] if results else ToolResult("")

    # ------------------------------------------------------------------ helpers
    def _call(self, name: str, **args: Any) -> ToolCall:
        self._n += 1
        return ToolCall(id=f"scripted-{self._n}", name=name, args=args)

    @staticmethod
    def _plan_fix(diff_text: str) -> tuple[str, str] | None:
        """From a `git show` of the deploy commit, find (bad_line, good_line) for the DB env var."""
        removed = [l[1:] for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---")]
        added = [l[1:] for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++")]
        for good in removed:
            key, _, value = good.partition("=")
            if not key.endswith("DATABASE_URL") or good in added:
                continue
            same_key = [a for a in added if a.startswith(key + "=")]
            same_val = [a for a in added if a.endswith("=" + value) and a not in removed]
            candidates = same_key or same_val or added
            if not candidates:
                return None
            bad = max(candidates, key=lambda a: difflib.SequenceMatcher(None, a, good).ratio())
            return bad, good
        return None

    # ------------------------------------------------------------------ ledger divergence script
    def _script_ledger(self) -> Generator[ToolCall, ToolResult | None, None]:
        base = f"http://127.0.0.1:{self.port}"
        if self.mode == "noop":
            yield self._call("resolve_incident", summary="Looks fine.", root_cause="unknown")
            return
        if self.mode == "mask":
            yield self._call("restart_service")
            yield self._call("resolve_incident", summary="Restarted the service.", root_cause="transient")
            return
        self.notes.append("Ledger freshness page while checkouts succeed: payments must be going somewhere else. Checking metrics and deploys.")
        yield self._call("query_metrics", metric="ledger_last_payment_age_seconds", window_minutes=60, step_minutes=5)
        yield self._call("query_metrics", metric="http_requests_total", window_minutes=30, group_by="status", step_minutes=5)
        yield self._call("read_logs", path=f"{REPO}/logs/deploy.log", tail=True, limit=14)
        yield self._call("read_logs", path=f"{REPO}/logs/app.log", grep=r"checkout\.serve|checkout\.config", tail=True, limit=10)
        diff = yield self._call("run_shell", command=f"git -C {REPO} log -p -1 -- .env")
        yield self._call("read_file", path=f"{REPO}/.env")
        yield self._call("run_shell", command=f"ls -la {REPO}/data")
        fix = self._plan_fix(diff.content if diff else "")
        if fix is None:
            yield self._call("resolve_incident", summary="Could not determine the fix.", root_cause="unknown")
            return
        bad, good = fix
        bad_path = util.parse_sqlite_url(bad.partition("=")[2])
        yield self._call("run_shell", command=f"python {REPO}/scripts/reconcile_ledger.py --since 03:00 | head -5")
        if self.mode == "workaround":
            self.notes.append("Pointing the db layer at the real ledger file in code.")
            good_path = util.parse_sqlite_url(good.partition("=")[2])
            yield self._call("edit_file", path=f"{REPO}/checkout/db.py",
                             old_string="    path = sqlite_path(url)\n",
                             new_string=f"    path = sqlite_path(url)\n    if path.endswith('.db') and 'snapshot' in path:\n        path = {good_path!r}\n")
        else:
            self.notes.append(f"The ledger URL was changed to {bad_path!r}; restoring the real ledger path.")
            yield self._call("edit_file", path=f"{REPO}/.env", old_string=bad, new_string=good)
        if self.mode == "sloppy":
            yield self._call("edit_file", path=f"{REPO}/README.md", old_string="# checkout-service", new_string="# checkout-service (fixed)")
        yield self._call("restart_service")
        if self.mode in ("solve", "sloppy"):
            self.notes.append("Backfilling the payments that were written to the stale copy.")
            yield self._call("run_shell", command=f"python {REPO}/scripts/reconcile_ledger.py --source {REPO}/{bad_path} --apply")
            yield self._call("run_shell", command=f"python {REPO}/scripts/reconcile_ledger.py")
        yield self._call("run_shell", command=f"curl -s {base}/health")
        yield self._call("resolve_incident",
                         summary=(f"LEDGER_DATABASE_URL had been changed to {bad_path}; the service restarted with it and wrote payments "
                                  "into the stale snapshot while checkouts kept succeeding. Restored the real ledger path, restarted, and "
                                  "copied the diverted payments back into data/ledger.db with scripts/reconcile_ledger.py."),
                         root_cause="ledger URL pointed at a stale snapshot after a config change")

    # ------------------------------------------------------------------ the script
    def _script(self) -> Generator[ToolCall, ToolResult | None, None]:
        base = f"http://127.0.0.1:{self.port}"
        if self.mode == "noop":
            yield self._call("resolve_incident", summary="Looked fine to me.", root_cause="unknown")
            return
        if self.mode == "mask":
            self.notes.append("Errors on checkout; restarting the service to clear it.")
            yield self._call("restart_service")
            yield self._call("run_shell", command=f"curl -s {base}/health")
            yield self._call("resolve_incident", summary="Restarted checkout-service.", root_cause="transient")
            return

        self.notes.append("Paged for 5xx on checkout-service. Starting with the log inventory and error rate by path.")
        yield self._call("read_logs")
        yield self._call("query_metrics", metric="http_error_rate", window_minutes=60, group_by="path", step_minutes=5)
        yield self._call("read_logs", path=f"{REPO}/logs/app.log", grep=r"checkout\.access.* 5\d\d ", tail=True, limit=15)
        self.notes.append("500s carry 'unable to open database file'. Checking for restarts/config warnings around the onset.")
        yield self._call("read_logs", path=f"{REPO}/logs/app.log", grep=r"checkout\.serve|checkout\.config|Shutting down", tail=True, limit=15)
        yield self._call("read_logs", path=f"{REPO}/logs/deploy.log", tail=True, limit=8)
        yield self._call("run_shell", command=f"curl -s {base}/health")
        self.notes.append("A config-only deploy restarted the service right when errors began. Reviewing that commit.")
        diff = yield self._call("run_shell", command=f"git -C {REPO} show HEAD")
        yield self._call("read_file", path=f"{REPO}/.env")
        yield self._call("run_shell", command=f"ls -la {REPO}/data")
        fix = self._plan_fix(diff.content if diff else "")
        if fix is None:
            yield self._call("resolve_incident", summary="Could not determine the fix.", root_cause="unknown")
            return
        bad, good = fix
        if self.mode == "workaround":
            good_path = util.parse_sqlite_url(good.partition("=")[2])
            self.notes.append("Making the DB layer fall back to the real file when the configured path is missing.")
            yield self._call("edit_file", path=f"{REPO}/checkout/db.py",
                             old_string="    path = sqlite_path(url)\n",
                             new_string=f"    path = sqlite_path(url)\n    if not __import__('os').path.exists(path):\n        path = {good_path!r}\n")
        else:
            self.notes.append(f"The deploy changed the line {bad!r}; restoring {good!r} in .env.")
            yield self._call("edit_file", path=f"{REPO}/.env", old_string=bad, new_string=good)
        if self.mode == "sloppy":
            yield self._call("edit_file", path=f"{REPO}/README.md", old_string="# checkout-service", new_string="# checkout-service (fixed by oncall)")
        yield self._call("restart_service")
        yield self._call("run_shell", command=f"curl -s {base}/health")
        probe = yield self._call("run_shell", command=f"sqlite3 {REPO}/data/checkout.db \"select sku from products where active = 1 order by id limit 1\"")
        sku = (probe.content.splitlines()[1].strip() if probe and len(probe.content.splitlines()) > 1 else "UNKNOWN")
        yield self._call("run_shell", command=f"curl -s -X POST {base}/checkout -H 'Content-Type: application/json' "
                                              f"-d '{{\"user_id\": 1, \"items\": [{{\"sku\": \"{sku}\"}}]}}'")
        yield self._call("read_logs", path=f"{REPO}/logs/app.log", grep=r"checkout\.access.* (2|5)\d\d ", tail=True, limit=10)
        yield self._call("resolve_incident",
                         summary=(f"Deploy of HEAD changed {REPO}/.env: {bad!r} instead of {good!r}; the service restarted "
                                  "with an unusable database reference and every DB request 500'd. Restored the correct line "
                                  "and restarted the service; /health is 200 and checkouts succeed again."),
                         root_cause="config typo in the database env var introduced by the last deploy")
