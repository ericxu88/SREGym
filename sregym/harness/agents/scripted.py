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
from sregym.generator.naming import CLASSIC
from sregym.harness.agents.base import AgentAdapter, AgentTurn, ToolCall
from sregym.tools.base import ToolResult


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
        self.naming = CLASSIC  # replaced by bind_world() with the generated world's stack identity

    def describe(self) -> dict[str, Any]:
        return {"agent": self.name, "mode": self.mode}

    def bind_world(self, world) -> None:  # noqa: ANN001
        self.naming = world.naming

    @property
    def repo(self) -> str:
        return self.naming.service

    @property
    def pkg(self) -> str:
        return self.naming.package

    @property
    def core_db(self) -> str:
        return self.naming.core_db_rel

    @property
    def checkout_route(self) -> str:
        return self.naming.checkout_route

    # ------------------------------------------------------------------ adapter API
    def start(self, system_prompt: str, task_prompt: str, tool_specs: list[dict[str, Any]]) -> None:
        m = re.search(r"http://127\.0\.0\.1:(\d+)", system_prompt)
        self.port = int(m.group(1)) if m else None
        task = task_prompt.lower()
        if "429" in task or "rate_limited" in task or "too many requests" in task:
            self._gen = self._script_ratelimit()
        elif "connection refused" in task or "no healthy upstream" in task or "down hard" in task:
            self._gen = self._script_crashloop()
        elif "ledger" in task:
            self._gen = self._script_ledger()
        else:
            self._gen = self._script()
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

    # ------------------------------------------------------------------ rate limit misconfig script
    def _script_ratelimit(self) -> Generator[ToolCall, ToolResult | None, None]:
        base = f"http://127.0.0.1:{self.port}"
        self.notes.append("429s with no 5xx: the rate limiter is rejecting legitimate retries. Checking its configuration.")
        yield self._call("read_logs", path=f"{self.repo}/logs/app.log", grep=r"ratelimit|429", tail=True, limit=10)
        env = yield self._call("read_file", path=f"{self.repo}/.env")
        yield self._call("run_shell", command=f"git -C {self.repo} log -p -1 -- .env | head -40")
        if self.mode == "mask":
            yield self._call("restart_service")
            yield self._call("resolve_incident", summary="Restarted.", root_cause="transient")
            return
        line = next((l.split("| ", 1)[1] for l in (env.content if env else "").splitlines() if "RATE_LIMIT_PER_MINUTE=" in l), None)
        if line is None:
            yield self._call("resolve_incident", summary="Could not find the limiter config.", root_cause="unknown")
            return
        if self.mode == "workaround":
            self.notes.append("Exempting retries in the limiter code.")
            yield self._call("edit_file", path=f"{self.repo}/{self.pkg}/main.py",
                             old_string="            n = self._counts.get((user_id, window), 0) + 1",
                             new_string="            n = 1  # hotfix: effectively disable the limiter")
            yield self._call("restart_service")
            yield self._call("resolve_incident", summary="Disabled the limiter in code.", root_cause="limiter")
            return
        self.notes.append(f"The deploy set {line.strip()!r}; the commit message says ~100 was intended. Restoring a sane limit.")
        yield self._call("edit_file", path=f"{self.repo}/.env", old_string=line.strip(), new_string="RATE_LIMIT_PER_MINUTE=100")
        if self.mode == "sloppy":
            yield self._call("edit_file", path=f"{self.repo}/README.md", old_string=f"# {self.repo}", new_string=f"# {self.repo} (fixed)")
        yield self._call("restart_service")
        probe = yield self._call("run_shell", command=f"sqlite3 {self.repo}/{self.core_db} 'select sku from products where active=1 limit 1'")
        sku = probe.content.splitlines()[1].strip() if probe and len(probe.content.splitlines()) > 1 else "X"
        body = f'{{"user_id": 1, "items": [{{"sku": "{sku}"}}]}}'
        yield self._call("run_shell", command="; ".join([f"curl -s -o /dev/null -w '%{{http_code}} ' -X POST {base}{self.checkout_route} -H 'Content-Type: application/json' -d '{body}'"] * 3))
        yield self._call("resolve_incident",
                         summary=("A config deploy set RATE_LIMIT_PER_MINUTE to a single-digit value (the commit message shows a much "
                                  "higher limit was intended), so normal checkout retries were 429'd. Restored a sane limit and "
                                  "restarted; rapid repeat checkouts succeed again."),
                         root_cause="checkout rate limit misconfigured by the last config deploy")

    # ------------------------------------------------------------------ dead service / crash loop script
    def _script_crashloop(self) -> Generator[ToolCall, ToolResult | None, None]:
        base = f"http://127.0.0.1:{self.port}"
        self.notes.append("Full outage with connection refused: the process is down. Checking its last words and the deploy trail.")
        yield self._call("run_shell", command=f"curl -s -m 3 {base}/health; echo exit=$?")
        tail = yield self._call("read_logs", path=f"{self.repo}/logs/app.log", tail=True, limit=25)
        yield self._call("read_logs", path=f"{self.repo}/logs/deploy.log", tail=True, limit=10)
        if self.mode == "mask":
            yield self._call("restart_service")
            yield self._call("resolve_incident", summary="Restarted.", root_cause="transient")
            return
        content = tail.content if tail else ""
        m = re.search(r"ImportError: cannot import name '(\w+)' from '(\w+)'", content)
        if not m:
            yield self._call("resolve_incident", summary="Could not find the crash cause.", root_cause="unknown")
            return
        symbol, pkg = m.group(1), m.group(2)
        yield self._call("run_shell", command=f"git -C {self.repo} log --oneline -3; git -C {self.repo} show HEAD")
        yield self._call("run_shell", command=f"cat {self.repo}/requirements.txt; ls {self.repo}/vendor/wheels")
        if self.mode == "workaround":
            self.notes.append(f"Adding {symbol} back to the installed package in lib/.")
            yield self._call("edit_file", path=f"{self.repo}/lib/{pkg}/__init__.py",
                             old_string='__version__ = "3.0.0"',
                             new_string=f'__version__ = "3.0.0"\n\n\ndef {symbol}(mapping) -> str:\n    return " ".join(f"{{k}}={{v}}" for k, v in dict(mapping).items())')
            yield self._call("restart_service")
            yield self._call("resolve_incident", summary=f"Re-added {symbol} to the installed package.", root_cause="bad dep")
            return
        self.notes.append(f"The deploy pinned {pkg} to a version that removed {symbol}(); restoring the previous pin and reinstalling.")
        yield self._call("edit_file", path=f"{self.repo}/requirements.txt", old_string=f"{pkg}==3.0.0", new_string=f"{pkg}==2.1.0")
        yield self._call("run_shell", command=f"python {self.repo}/scripts/deploy_deps.py")
        if self.mode == "sloppy":
            yield self._call("edit_file", path=f"{self.repo}/README.md", old_string=f"# {self.repo}", new_string=f"# {self.repo} (fixed)")
        yield self._call("restart_service")
        yield self._call("run_shell", command=f"curl -s {base}/health")
        yield self._call("resolve_incident",
                         summary=(f"Release at HEAD bumped {pkg} to 3.0.0, whose API removed {symbol}(); the service crashed at import "
                                  "on restart and hit the start limit, taking the site down. Restored the 2.1.0 pin, reinstalled from "
                                  "the wheelhouse with scripts/deploy_deps.py, and restarted; health and checkouts are green."),
                         root_cause="dependency pin bumped to an incompatible version")

    # ------------------------------------------------------------------ db file permissions script
    def _script_disk_full(self) -> Generator[ToolCall, ToolResult | None, None]:
        base = f"http://127.0.0.1:{self.port}"
        self.notes.append("'database or disk is full' but df shows plenty. Checking quotas/config around the onset.")
        yield self._call("run_shell", command=f"df -k .; ls -la {self.repo}/data")
        yield self._call("read_logs", path=f"{self.repo}/logs/deploy.log", tail=True, limit=8)
        diff = yield self._call("run_shell", command=f"git -C {self.repo} show HEAD")
        env = yield self._call("read_file", path=f"{self.repo}/.env")
        line = next((l.split("| ", 1)[1].strip() for l in (env.content if env else "").splitlines()
                     if "DATABASE_MAX_PAGES=" in l), None)
        if line is None:
            yield self._call("resolve_incident", summary="Could not find the quota.", root_cause="unknown")
            return
        if self.mode == "workaround":
            self.notes.append("Patching the guardrail out of the DB layer.")
            yield self._call("edit_file", path=f"{self.repo}/{self.pkg}/db.py",
                             old_string="    if settings.database_max_pages and url == settings.database_url:",
                             new_string="    if False and settings.database_max_pages and url == settings.database_url:")
        else:
            self.notes.append(f"The last deploy added {line!r}, below the db's current page count. Removing it.")
            yield self._call("edit_file", path=f"{self.repo}/.env", old_string=f"\n{line}", new_string="")
        if self.mode == "sloppy":
            yield self._call("edit_file", path=f"{self.repo}/README.md", old_string=f"# {self.repo}", new_string=f"# {self.repo} (fixed)")
        yield self._call("restart_service")
        probe = yield self._call("run_shell", command=f"sqlite3 {self.repo}/{self.core_db} 'select sku from products where active=1 limit 1'")
        sku = probe.content.splitlines()[1].strip() if probe and len(probe.content.splitlines()) > 1 else "X"
        body = f'{{"user_id": 1, "items": [{{"sku": "{sku}"}}]}}'
        yield self._call("run_shell", command="; ".join([f"curl -s -o /dev/null -w '%{{http_code}} ' -X POST {base}{self.checkout_route} -H 'Content-Type: application/json' -d '{body}'"] * 3))
        yield self._call("resolve_incident",
                         summary=("A guardrail deploy set DATABASE_MAX_PAGES below the core database's current size, so "
                                  "SQLite failed every write with 'database or disk is full' (disk space was fine). "
                                  "Removed the quota from .env and restarted; writes succeed again."),
                         root_cause="DATABASE_MAX_PAGES set below the core db's current page count by the last config deploy")

    def _script_permissions(self) -> Generator[ToolCall, ToolResult | None, None]:
        base = f"http://127.0.0.1:{self.port}"
        self.notes.append("Writes fail with 'readonly database' while reads work: checking file modes and the host agent log.")
        yield self._call("read_logs", path="var/log/fleetd.log", tail=True, limit=8)
        listing = yield self._call("run_shell", command=f"ls -la {self.repo}/data && ls -ld {self.repo}/data")
        if self.mode == "mask":
            yield self._call("restart_service")
            yield self._call("resolve_incident", summary="Restarted.", root_cause="transient")
            return
        target = None
        for line in (listing.content if listing else "").splitlines():
            cols = line.split()
            if not cols or not cols[0].startswith(("d", "-")):
                continue
            mode, name = cols[0], cols[-1]
            if "w" not in mode[1:4]:
                if mode.startswith("d"):
                    target = ("dir", f"{self.repo}/data" if name.endswith("/data") or name == "." else f"{self.repo}/data")
                elif name.endswith(".db"):
                    target = ("file", f"{self.repo}/data/{name}")
                if target:
                    break
        if target is None:
            yield self._call("resolve_incident", summary="Could not find the read-only path.", root_cause="unknown")
            return
        kind, path = target
        if self.mode == "workaround":
            self.notes.append("Repointing the app at a fresh database path it can write to.")
            yield self._call("edit_file", path=f"{self.repo}/.env", old_string=f"DATABASE_URL=sqlite:///{self.core_db}",
                             new_string="DATABASE_URL=sqlite:///run/checkout-rw.db")
            yield self._call("restart_service")
            yield self._call("resolve_incident", summary="Moved the db path.", root_cause="disk perms")
            return
        self.notes.append(f"fleetd hardening made {path} read-only; restoring owner write.")
        yield self._call("run_shell", command=f"chmod {'755' if kind == 'dir' else '644'} {path}")
        if self.mode == "sloppy":
            yield self._call("edit_file", path=f"{self.repo}/README.md", old_string=f"# {self.repo}", new_string=f"# {self.repo} (fixed)")
        yield self._call("run_shell", command=f"ls -ld {path}")
        probe = yield self._call("run_shell", command=f"sqlite3 {self.repo}/{self.core_db} 'select sku from products where active=1 limit 1'")
        sku = probe.content.splitlines()[1].strip() if probe and len(probe.content.splitlines()) > 1 else "X"
        yield self._call("run_shell", command=f"curl -s -X POST {base}{self.checkout_route} -H 'Content-Type: application/json' "
                                              f"-d '{{\"user_id\": 1, \"items\": [{{\"sku\": \"{sku}\"}}]}}'")
        yield self._call("resolve_incident",
                         summary=(f"fleetd's permissions baseline made {path} read-only at the incident time; SQLite kept serving reads "
                                  "but every write failed with 'attempt to write a readonly database'. Restored owner write with chmod; "
                                  "checkouts confirm again. No restart or code change needed."),
                         root_cause="hardening policy removed the write bit from the service data path")

    # ------------------------------------------------------------------ cron write lock script
    def _script_cron_lock(self) -> Generator[ToolCall, ToolResult | None, None]:
        self.notes.append("'database is locked' in bursts: something is holding the write lock periodically. Checking cron.")
        yield self._call("read_logs", path=f"{self.repo}/logs/cron.log", tail=True, limit=12)
        cron = yield self._call("run_shell", command=f"cat etc/cron.d/{self.repo}; ls -la etc/cron.d")
        if self.mode == "mask":
            yield self._call("restart_service")
            yield self._call("resolve_incident", summary="Restarted the service.", root_cause="transient lock contention")
            return
        if self.mode == "workaround":
            self.notes.append("Making the archive job commit per batch so it stops holding the lock.")
            yield self._call("edit_file", path=f"{self.repo}/scripts/archive_orders.py",
                             old_string="        conn.execute(\"BEGIN IMMEDIATE\")",
                             new_string="        # conn.execute(\"BEGIN IMMEDIATE\")  # hotfix: do not hold the write lock")
            yield self._call("resolve_incident", summary="Patched the job to not hold the lock.", root_cause="archive job")
            return
        line = next((l for l in (cron.content if cron else "").splitlines() if "archive_orders" in l and not l.strip().startswith("#")), None)
        if line is None:
            yield self._call("resolve_incident", summary="Could not find the job.", root_cause="unknown")
            return
        self.notes.append("An every-minute archive backfill was added to cron; each run holds the write lock. Disabling it.")
        yield self._call("edit_file", path=f"etc/cron.d/{self.repo}", old_string=line,
                         new_string="# DISABLED during INC (held the core db write lock every minute): " + line.lstrip("$ "))
        if self.mode == "sloppy":
            yield self._call("edit_file", path=f"{self.repo}/README.md", old_string=f"# {self.repo}", new_string=f"# {self.repo} (fixed)")
        yield self._call("read_logs", path=f"{self.repo}/logs/cron.log", tail=True, limit=4)
        yield self._call("resolve_incident",
                         summary=("A cron entry added to etc/cron.d ran scripts/archive_orders.py every minute; each run held a "
                                  f"~30s write transaction on {self.core_db}, so checkout writes hit the 5s busy timeout in bursts. "
                                  "Commented the entry out; no restart or code change needed."),
                         root_cause="every-minute archive cron job holding the core db write lock")

    # ------------------------------------------------------------------ unapplied migration script
    _MIGRATION_KNOWLEDGE = {  # test-oracle knowledge of the three feature variants (column -> table, full column set, name)
        "coupon_code": ("orders", [("coupon_code", "TEXT"), ("discount_cents", "INTEGER NOT NULL DEFAULT 0")], "003_coupons"),
        "fulfillment_status": ("orders", [("fulfillment_status", "TEXT NOT NULL DEFAULT 'unfulfilled'")], "003_fulfillment"),
        "marketing_opt_in": ("users", [("marketing_opt_in", "INTEGER NOT NULL DEFAULT 0")], "003_marketing_optin"),
    }

    def _script_migration(self, log_excerpt: str) -> Generator[ToolCall, ToolResult | None, None]:
        base = f"http://127.0.0.1:{self.port}"
        m = re.search(r"no such column: (\w+)|has no column named (\w+)", log_excerpt)
        column = (m.group(1) or m.group(2)) if m else None
        self.notes.append(f"Errors say the schema lacks column {column!r}: a deploy shipped code ahead of its migration.")
        yield self._call("run_shell", command=f"git -C {self.repo} log --oneline -3 && git -C {self.repo} show HEAD --stat")
        yield self._call("run_shell", command=f"ls -la {self.repo}/migrations")
        if self.mode == "mask":
            yield self._call("restart_service")
            yield self._call("resolve_incident", summary="Restarted.", root_cause="transient")
            return
        if self.mode == "workaround":
            self.notes.append("Removing the new column from the query to stop the errors.")
            main = f"{self.repo}/{self.pkg}/main.py"
            r = yield self._call("read_file", path=main, start_line=1, max_lines=200)
            line = None
            for l in (r.content if r else "").splitlines():
                body = l.split("| ", 1)[1] if "| " in l else ""
                if column and f", {column}" in body and body.strip().endswith('"'):
                    line = body
                    break
            if line:
                yield self._call("edit_file", path=main, old_string=line + "\n", new_string="")
            yield self._call("restart_service")
            yield self._call("resolve_incident", summary="Removed the new column from the query.", root_cause="bad deploy")
            return
        status = yield self._call("run_shell", command=f"python {self.repo}/scripts/migrate.py")
        if status and "PENDING" in status.content:
            self.notes.append("A migration is pending; applying it.")
        else:
            table, cols, name = self._MIGRATION_KNOWLEDGE.get(column or "", ("orders", [], "003_missing"))
            self.notes.append("No migration file was shipped for this column; writing it from the feature's needs.")
            sql = "".join(f"ALTER TABLE {table} ADD COLUMN {c} {t};\n" for c, t in cols)
            sql += f"INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES ('{name}', strftime('%Y-%m-%dT%H:%M:%SZ','now'));\n"
            yield self._call("edit_file", path=f"{self.repo}/migrations/{name}.sql", old_string="", new_string=f"-- {name}: written during incident response\n" + sql)
        yield self._call("run_shell", command=f"python {self.repo}/scripts/migrate.py --apply")
        if self.mode == "sloppy":
            yield self._call("edit_file", path=f"{self.repo}/README.md", old_string=f"# {self.repo}", new_string=f"# {self.repo} (fixed)")
        yield self._call("run_shell", command=f"curl -s -o /dev/null -w '%{{http_code}}' {base}/orders/1 && curl -s -o /dev/null -w ' %{{http_code}}' {base}/users/1")
        yield self._call("read_logs", path=f"{self.repo}/logs/app.log", grep=rf"{self.pkg}\.access.* 5\d\d ", tail=True, limit=3)
        yield self._call("resolve_incident",
                         summary=(f"Release at HEAD referenced {column} but its schema migration had not been applied to the production "
                                  "database (deploy-bot does not run migrations). Applied the migration with scripts/migrate.py; the "
                                  "affected endpoints recovered without a restart."),
                         root_cause="schema migration not applied after deploy")

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
        yield self._call("read_logs", path=f"{self.repo}/logs/deploy.log", tail=True, limit=14)
        yield self._call("read_logs", path=f"{self.repo}/logs/app.log", grep=rf"{self.pkg}\.serve|{self.pkg}\.config", tail=True, limit=10)
        diff = yield self._call("run_shell", command=f"git -C {self.repo} log -p -1 -- .env")
        yield self._call("read_file", path=f"{self.repo}/.env")
        yield self._call("run_shell", command=f"ls -la {self.repo}/data")
        fix = self._plan_fix(diff.content if diff else "")
        if fix is None:
            yield self._call("resolve_incident", summary="Could not determine the fix.", root_cause="unknown")
            return
        bad, good = fix
        bad_path = util.parse_sqlite_url(bad.partition("=")[2])
        yield self._call("run_shell", command=f"python {self.repo}/scripts/reconcile_ledger.py --since 03:00 | head -5")
        if self.mode == "workaround":
            self.notes.append("Pointing the db layer at the real ledger file in code.")
            good_path = util.parse_sqlite_url(good.partition("=")[2])
            yield self._call("edit_file", path=f"{self.repo}/{self.pkg}/db.py",
                             old_string="    path = sqlite_path(url)\n",
                             new_string=f"    path = sqlite_path(url)\n    if path.endswith('.db') and 'snapshot' in path:\n        path = {good_path!r}\n")
        else:
            self.notes.append(f"The ledger URL was changed to {bad_path!r}; restoring the real ledger path.")
            yield self._call("edit_file", path=f"{self.repo}/.env", old_string=bad, new_string=good)
        if self.mode == "sloppy":
            yield self._call("edit_file", path=f"{self.repo}/README.md", old_string=f"# {self.repo}", new_string=f"# {self.repo} (fixed)")
        yield self._call("restart_service")
        if self.mode in ("solve", "sloppy"):
            self.notes.append("Backfilling the payments that were written to the stale copy.")
            yield self._call("run_shell", command=f"python {self.repo}/scripts/reconcile_ledger.py --source {self.repo}/{bad_path} --apply")
            yield self._call("run_shell", command=f"python {self.repo}/scripts/reconcile_ledger.py")
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
            yield self._call("resolve_incident", summary=f"Restarted {self.repo}.", root_cause="transient")
            return

        self.notes.append(f"Paged for 5xx on {self.repo}. Starting with the log inventory and error rate by path.")
        yield self._call("read_logs")
        yield self._call("query_metrics", metric="http_error_rate", window_minutes=60, group_by="path", step_minutes=5)
        r = yield self._call("read_logs", path=f"{self.repo}/logs/app.log", grep=rf"{self.pkg}\.access.* 5\d\d ", tail=True, limit=15)
        if r and ("no such column" in r.content or "has no column named" in r.content):
            yield from self._script_migration(r.content)
            return
        if r and "database is locked" in r.content:
            yield from self._script_cron_lock()
            return
        if r and "attempt to write a readonly database" in r.content:
            yield from self._script_permissions()
            return
        if r and "database or disk is full" in r.content:
            yield from self._script_disk_full()
            return
        self.notes.append("500s carry 'unable to open database file'. Checking for restarts/config warnings around the onset.")
        yield self._call("read_logs", path=f"{self.repo}/logs/app.log", grep=rf"{self.pkg}\.serve|{self.pkg}\.config|Shutting down", tail=True, limit=15)
        yield self._call("read_logs", path=f"{self.repo}/logs/deploy.log", tail=True, limit=8)
        yield self._call("run_shell", command=f"curl -s {base}/health")
        self.notes.append("A config-only deploy restarted the service right when errors began. Reviewing that commit.")
        diff = yield self._call("run_shell", command=f"git -C {self.repo} show HEAD")
        yield self._call("read_file", path=f"{self.repo}/.env")
        yield self._call("run_shell", command=f"ls -la {self.repo}/data")
        fix = self._plan_fix(diff.content if diff else "")
        if fix is None:
            yield self._call("resolve_incident", summary="Could not determine the fix.", root_cause="unknown")
            return
        bad, good = fix
        if self.mode == "workaround":
            good_path = util.parse_sqlite_url(good.partition("=")[2])
            self.notes.append("Making the DB layer fall back to the real file when the configured path is missing.")
            yield self._call("edit_file", path=f"{self.repo}/{self.pkg}/db.py",
                             old_string="    path = sqlite_path(url)\n",
                             new_string=f"    path = sqlite_path(url)\n    if not __import__('os').path.exists(path):\n        path = {good_path!r}\n")
        else:
            self.notes.append(f"The deploy changed the line {bad!r}; restoring {good!r} in .env.")
            yield self._call("edit_file", path=f"{self.repo}/.env", old_string=bad, new_string=good)
        if self.mode == "sloppy":
            yield self._call("edit_file", path=f"{self.repo}/README.md", old_string=f"# {self.repo}", new_string=f"# {self.repo} (fixed by oncall)")
        yield self._call("restart_service")
        yield self._call("run_shell", command=f"curl -s {base}/health")
        probe = yield self._call("run_shell", command=f"sqlite3 {self.repo}/{self.core_db} \"select sku from products where active = 1 order by id limit 1\"")
        sku = (probe.content.splitlines()[1].strip() if probe and len(probe.content.splitlines()) > 1 else "UNKNOWN")
        yield self._call("run_shell", command=f"curl -s -X POST {base}{self.checkout_route} -H 'Content-Type: application/json' "
                                              f"-d '{{\"user_id\": 1, \"items\": [{{\"sku\": \"{sku}\"}}]}}'")
        yield self._call("read_logs", path=f"{self.repo}/logs/app.log", grep=rf"{self.pkg}\.access.* (2|5)\d\d ", tail=True, limit=10)
        yield self._call("resolve_incident",
                         summary=(f"Deploy of HEAD changed {self.repo}/.env: {bad!r} instead of {good!r}; the service restarted "
                                  "with an unusable database reference and every DB request 500'd. Restored the correct line "
                                  "and restarted the service; /health is 200 and checkouts succeed again."),
                         root_cause="config typo in the database env var introduced by the last deploy")
