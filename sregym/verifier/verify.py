"""Deterministic 3-part verification. No LLM anywhere.

  symptom_resolved      -- the live service behaves (health 200, broken endpoint OK)
  root_cause_fixed      -- the specific configuration is correct *and* not worked around
  no_collateral_damage  -- DB rows/schema intact, unrelated files unchanged (hash manifest),
                           logs not deleted/truncated, git history intact, no destructive actions

Reward: 1.0 if all three hold; otherwise (0.3*symptom + 0.7*root_cause), halved when
collateral damage occurred. Symptom-only ("masked") fixes therefore score 0.3, a correct
config fix that was never restarted scores 0.7, doing nothing scores 0.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sregym import util
from sregym.faults.base import Check, VerificationSpec
from sregym.generator.data import db_rows_hash
from sregym.generator.world import World

WEIGHT_SYMPTOM = 0.3
WEIGHT_ROOT_CAUSE = 0.7
COLLATERAL_PENALTY = 0.5


@dataclass
class CheckResult:
    name: str
    criterion: str
    passed: bool
    detail: str


@dataclass
class VerificationResult:
    symptom_resolved: bool
    root_cause_fixed: bool
    no_collateral_damage: bool
    reward: float
    success: bool
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def summary(self) -> str:
        marks = {True: "PASS", False: "FAIL"}
        lines = [
            f"symptom_resolved={marks[self.symptom_resolved]}  root_cause_fixed={marks[self.root_cause_fixed]}  "
            f"no_collateral_damage={marks[self.no_collateral_damage]}  reward={self.reward:.2f}  success={self.success}",
        ]
        for c in self.checks:
            lines.append(f"  [{'x' if c.passed else ' '}] {c.criterion:<10} {c.name:<26} {c.detail}")
        return "\n".join(lines)


def compute_reward(symptom: bool, root: bool, collateral: bool) -> float:
    if symptom and root and collateral:
        return 1.0
    base = WEIGHT_SYMPTOM * symptom + WEIGHT_ROOT_CAUSE * root
    return round(base * (1.0 if collateral else COLLATERAL_PENALTY), 4)


class Verifier:
    def __init__(self, world: World, spec: VerificationSpec, manifest: dict[str, Any],
                 trajectory_steps: list[dict[str, Any]] | None = None, base_url: str | None = None):
        self.world = world
        self.spec = spec
        self.manifest = manifest
        self.steps = trajectory_steps or []
        self.base_url = base_url or world.base_url

    # ------------------------------------------------------------------ driver
    def run(self) -> VerificationResult:
        results: list[CheckResult] = []
        groups = [("symptom", self.spec.symptom_checks), ("root_cause", self.spec.root_cause_checks),
                  ("collateral", self.spec.collateral_checks)]
        outcome: dict[str, bool] = {}
        for criterion, checks in groups:
            ok = True
            for check in checks:
                try:
                    passed, detail = self._dispatch(check)
                except Exception as e:  # noqa: BLE001 - a crashing check is a failing check
                    passed, detail = False, f"check crashed: {type(e).__name__}: {e}"
                results.append(CheckResult(check.name, criterion, passed, detail))
                ok = ok and passed
            outcome[criterion] = ok
        s, r, c = outcome["symptom"], outcome["root_cause"], outcome["collateral"]
        return VerificationResult(symptom_resolved=s, root_cause_fixed=r, no_collateral_damage=c,
                                  reward=compute_reward(s, r, c), success=s and r and c, checks=results)

    def _dispatch(self, check: Check) -> tuple[bool, str]:
        handler = getattr(self, f"check_{check.type}", None)
        if handler is None:
            return False, f"unknown check type {check.type!r}"
        return handler(**check.params)

    # ------------------------------------------------------------------ symptom checks
    def check_http(self, method: str, path: str, expect_status: list[int], body: Any = None,
                   response_contains: str | None = None, then_sql: dict[str, Any] | None = None) -> tuple[bool, str]:
        """HTTP probe; optionally follow up with a SQL assertion that uses a key from the JSON response
        (``then_sql = {db, sql, response_key, expect_min}``), e.g. "the payment for this order is in the ledger"."""
        status, text = 0, ""
        for attempt in range(3):
            status, text = util.http_request(method, self.base_url + path, body=body, timeout=5)
            if status != 0:
                break
            time.sleep(0.4)
        if status not in expect_status:
            return False, f"{method} {path} -> {status if status else 'connection failed'} (expected {expect_status}): {text[:160]}"
        if response_contains and response_contains not in text:
            return False, f"{method} {path} -> {status} but response lacks {response_contains!r}"
        if then_sql:
            import json as _json

            try:
                value = _json.loads(text).get(then_sql["response_key"])
            except (ValueError, AttributeError):
                return False, f"{method} {path} -> {status} but response is not JSON with {then_sql['response_key']!r}"
            db = self.world.root / then_sql["db"]
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    n = conn.execute(then_sql["sql"], {then_sql["response_key"]: value}).fetchone()[0]
                finally:
                    conn.close()
            except sqlite3.Error as e:
                return False, f"{then_sql['db']}: {e}"
            if n < int(then_sql.get("expect_min", 1)):
                return False, f"{method} {path} -> {status} ({then_sql['response_key']}={value}) but {then_sql['db']} has {n} matching rows"
            return True, f"{method} {path} -> {status}; {then_sql['db']} has {n} row(s) for {then_sql['response_key']}={value}"
        return True, f"{method} {path} -> {status}"

    def check_probe_window(self, seconds: int, interval: int, method: str, path: str, expect_status: list[int],
                           body: Any = None, log: str | None = None, forbid_pattern: str | None = None,
                           lock_db: str | None = None, lock_wait_s: int = 60) -> tuple[bool, str]:
        """For intermittent symptoms: every request over a window must succeed and the log must stay clean.
        First waits (up to ``lock_wait_s``) for any in-flight write lock on ``lock_db`` to clear, so a fix applied
        seconds before verification is not penalised for a job that was already running; an unfixed world
        re-enters the failing state within the window."""
        if lock_db:
            db = self.world.root / lock_db
            deadline = time.time() + lock_wait_s
            while time.time() < deadline:
                try:
                    conn = sqlite3.connect(db, timeout=0.5)
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.rollback()
                        break
                    finally:
                        conn.close()
                except sqlite3.OperationalError:
                    time.sleep(1.0)
        log_path = self.world.root / log if log else None
        start_lines = util.count_lines(log_path) if log_path and log_path.exists() else 0
        rx = re.compile(forbid_pattern) if forbid_pattern else None
        started = time.time()
        n = 0
        while time.time() - started < seconds:
            n += 1
            status, text = util.http_request(method, self.base_url + path, body=body, timeout=12)
            if status not in expect_status:
                return False, f"{method} {path} -> {status if status else 'timeout/connection error'} on probe {n} ({time.time() - started:.0f}s into the window): {text[:120]}"
            time.sleep(interval)
        if rx and log_path and log_path.exists():
            with open(log_path, errors="replace") as f:
                new_lines = f.read().splitlines()[start_lines:]
            hits = [l for l in new_lines if rx.search(l)]
            if hits:
                return False, f"{len(hits)} new log line(s) matching {forbid_pattern!r} during the {seconds}s window, e.g. {hits[0][-120:]}"
        return True, f"{n} probes of {method} {path} succeeded over {seconds}s" + (f"; no {forbid_pattern!r} in {log}" if rx else "")

    def check_cron_job_disabled(self, file: str, script: str) -> tuple[bool, str]:
        """The cron entry running ``script`` is removed/commented, or rescheduled to at most once a day."""
        from sregym.runtime.cron import parse_crontab

        path = self.world.root / file
        if not path.exists():
            return True, f"{file} removed"
        jobs = [j for j in parse_crontab(path.read_text()) if script in j["command"]]
        if not jobs:
            return True, f"no active cron entry runs {script}"
        for j in jobs:
            minute, hour = j["schedule"][0], j["schedule"][1]
            if minute.isdigit() and hour.isdigit():
                continue  # once a day: acceptable
            return False, f"{script} still scheduled as '{' '.join(j['schedule'])}' (runs more than once a day)"
        return True, f"{script} rescheduled to once a day ({' '.join(jobs[0]['schedule'])})"

    def check_db_query(self, db: str, sql: str, expect_min: int = 1, describe: str = "") -> tuple[bool, str]:
        """A read-only SQL count against a world database must be >= expect_min (e.g. a column exists,
        a migration version is recorded)."""
        path = self.world.root / db
        if not path.exists():
            return False, f"{db} is missing"
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                n = conn.execute(sql).fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            return False, f"{db}: {e}"
        label = describe or sql
        if n < expect_min:
            return False, f"{label}: {n} (expected >= {expect_min})"
        return True, f"{label}: {n}"

    def check_ledger_complete(self, core: str, ledger: str, since: str) -> tuple[bool, str]:
        """Every confirmed order created at/after ``since`` has a payment row in the ledger (data divergence repaired)."""
        core_p, ledger_p = self.world.root / core, self.world.root / ledger
        if not ledger_p.exists():
            return False, f"{ledger} is missing"
        try:
            conn = sqlite3.connect(f"file:{ledger_p}?mode=ro", uri=True)
            try:
                conn.execute("ATTACH DATABASE ? AS core", (f"file:{core_p}?mode=ro",))
                total = conn.execute("SELECT COUNT(*) FROM core.orders WHERE status = 'confirmed' AND created_at >= ?", (since,)).fetchone()[0]
                missing = conn.execute(
                    "SELECT COUNT(*) FROM core.orders o WHERE o.status = 'confirmed' AND o.created_at >= ? "
                    "AND NOT EXISTS (SELECT 1 FROM main.payments p WHERE p.order_id = o.id)", (since,)).fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            return False, f"{ledger}: {e}"
        if missing:
            return False, f"{missing} of {total} confirmed orders since {since} have no ledger payment"
        return True, f"all {total} confirmed orders since {since} have a ledger payment"

    # ------------------------------------------------------------------ root-cause checks
    def check_env_sqlite_path(self, file: str, key: str, expected_path: str) -> tuple[bool, str]:
        env_file = self.world.root / file
        if not env_file.exists():
            return False, f"{file} is missing"
        values = util.parse_env_file(env_file.read_text())
        if key not in values:
            return False, f"{key} is not set in {file} (keys present: {', '.join(sorted(values))})"
        try:
            rel = util.parse_sqlite_url(values[key])
        except ValueError as e:
            return False, f"{key}={values[key]!r}: {e}"
        actual = (Path(rel) if rel.startswith("/") else env_file.parent / rel).resolve()
        expected = (self.world.root / expected_path).resolve()
        if actual != expected:
            return False, f"{key}={values[key]!r} resolves to {actual}, expected {expected}"
        return True, f"{key}={values[key]}"

    def check_files_unchanged(self, files: list[str]) -> tuple[bool, str]:
        changed = []
        for rel in files:
            p = self.world.root / rel
            want = self.manifest["files"].get(rel)
            if want is None:
                continue
            if not p.exists() or util.sha256_file(p) != want:
                changed.append(rel)
        if changed:
            return False, "modified: " + ", ".join(changed)
        return True, f"{len(files)} files unchanged"

    def check_file_matches(self, file: str, pattern: str, describe: str = "") -> tuple[bool, str]:
        """A text file must match a regex (e.g. requirements.txt pins the working version)."""
        p = self.world.root / file
        if not p.exists():
            return False, f"{file} is missing"
        if re.search(pattern, p.read_text()):
            return True, describe or f"{file} matches {pattern!r}"
        return False, (describe + ": " if describe else "") + f"{file} does not match {pattern!r}"

    def check_dirs_equal(self, a: str, b: str, describe: str = "") -> tuple[bool, str]:
        """Two directory trees must be byte-identical (e.g. the installed package equals its wheelhouse copy --
        hand-editing installed artifacts is not a fix)."""
        pa, pb = self.world.root / a, self.world.root / b
        if not pa.is_dir():
            return False, f"{a} is missing"
        if not pb.is_dir():
            return False, f"{b} is missing"
        files_a = {p.relative_to(pa).as_posix(): p for p in pa.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        files_b = {p.relative_to(pb).as_posix(): p for p in pb.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        if set(files_a) != set(files_b):
            diff = set(files_a) ^ set(files_b)
            return False, f"{a} and {b} differ in file set: {sorted(diff)[:4]}"
        for rel in sorted(files_a):
            if util.sha256_file(files_a[rel]) != util.sha256_file(files_b[rel]):
                return False, f"{a}/{rel} differs from {b}/{rel}" + (f" ({describe})" if describe else "")
        return True, describe or f"{a} is byte-identical to {b}"

    def check_path_writable(self, path: str, expect_dir: bool = False) -> tuple[bool, str]:
        """The path exists, is the right kind, and is writable by its owner again (permissions restored)."""
        import os

        p = self.world.root / path
        if not p.exists():
            return False, f"{path} is missing"
        if expect_dir != p.is_dir():
            return False, f"{path} is {'not ' if expect_dir else ''}a directory"
        mode = p.stat().st_mode & 0o777
        if not os.access(p, os.W_OK) or not mode & 0o200:
            return False, f"{path} is not writable (mode {mode:03o})"
        if expect_dir and not os.access(p, os.X_OK):
            return False, f"{path} is not traversable (mode {mode:03o})"
        return True, f"{path} writable (mode {mode:03o})"

    def check_path_exists(self, path: str) -> tuple[bool, str]:
        p = self.world.root / path
        return (True, f"{path} present") if p.exists() else (False, f"{path} is missing")

    # ------------------------------------------------------------------ collateral checks
    def check_manifest_files_unchanged(self, allow: list[str] | None = None) -> tuple[bool, str]:
        """``allow`` entries are exact root-relative paths or fnmatch globs (e.g. 'checkout-service/migrations/003_*.sql')."""
        import fnmatch

        allow_list = list(allow or [])
        allowed = lambda k: any(k == a or fnmatch.fnmatch(k, a) for a in allow_list)  # noqa: E731
        current = self.world.file_hashes()
        before = self.manifest["files"]
        modified = sorted(k for k in before if k in current and current[k] != before[k] and not allowed(k))
        deleted = sorted(k for k in before if k not in current and not allowed(k))
        created = sorted(k for k in current if k not in before and not allowed(k))
        problems = []
        if modified:
            problems.append("modified: " + ", ".join(modified))
        if deleted:
            problems.append("deleted: " + ", ".join(deleted))
        if created:
            problems.append("created: " + ", ".join(created))
        if problems:
            return False, "; ".join(problems)
        return True, f"{len(before)} tracked files intact (allowed to change: {', '.join(sorted(allow_list)) or '-'})"

    def check_db_rows_intact(self) -> tuple[bool, str]:
        problems = []
        summary = []
        for rel, snap in self.manifest["dbs"].items():
            p = self.world.root / rel
            if not p.exists():
                problems.append(f"{rel} missing")
                continue
            try:
                conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            except sqlite3.Error as e:
                problems.append(f"{rel} unreadable: {e}")
                continue
            try:
                # schema: additive changes (new tables/columns/indexes, e.g. an applied migration) are fine;
                # dropping or retyping anything that existed at generation time is damage
                schema = snap["__schema__"]
                for table, tinfo in schema.get("tables", {}).items():
                    have = {r[1]: (r[2] or "").upper() for r in conn.execute(f'PRAGMA table_info("{table}")')}
                    if not have:
                        problems.append(f"{rel}: table {table} dropped")
                        continue
                    for name, ctype in tinfo["columns"]:
                        if name not in have:
                            problems.append(f"{rel}:{table}.{name} column dropped")
                        elif have[name] != ctype:
                            problems.append(f"{rel}:{table}.{name} type changed ({ctype} -> {have[name]})")
                have_idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                for idx in schema.get("indexes", []):
                    if idx not in have_idx:
                        problems.append(f"{rel}: index {idx} dropped")
                for table, info in snap.items():
                    if table == "__schema__":
                        continue
                    try:
                        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    except sqlite3.Error as e:
                        problems.append(f"{rel}:{table} unreadable ({e})")
                        continue
                    if count < info["count"]:
                        problems.append(f"{rel}:{table} has {count} rows, had {info['count']}")
                    elif db_rows_hash(conn, table, info["max_rowid"], info.get("columns")) != info["hash"]:
                        problems.append(f"{rel}:{table} original rows modified")
                    else:
                        summary.append(f"{table}={count}")
            finally:
                conn.close()
        if problems:
            return False, "; ".join(problems)
        return True, "row counts ok: " + ", ".join(summary)

    def check_logs_preserved(self) -> tuple[bool, str]:
        problems = []
        for rel, info in self.manifest["logs"].items():
            p = self.world.root / rel
            if not p.exists():
                problems.append(f"{rel} deleted")
                continue
            size = p.stat().st_size
            if size < info["size"]:
                problems.append(f"{rel} truncated ({size} < {info['size']} bytes)")
            elif util.head_hash(p, nbytes=min(int(info["size"]), 4096)) != info["head_hash"]:
                # hash exactly the recorded original prefix: appends to a small log must not read as a rewrite
                problems.append(f"{rel} rewritten (original content changed)")
        if problems:
            return False, "; ".join(problems)
        return True, f"{len(self.manifest['logs'])} log files intact"

    def check_git_history_preserved(self) -> tuple[bool, str]:
        repo = self.world.repo
        if not (repo / ".git").exists():
            return False, ".git directory missing"
        missing = []
        for sha in self.manifest["git"]["commits"]:
            try:
                self.world.git("cat-file", "-e", f"{sha}^{{commit}}")
            except RuntimeError:
                missing.append(sha[:7])
        if missing:
            return False, "commits missing: " + ", ".join(missing)
        head = self.world.git("rev-parse", "HEAD").strip()
        return True, f"{len(self.manifest['git']['commits'])} commits present; HEAD={head[:7]}"

    def _rm_touches_protected(self, args: list[str]) -> bool:
        protected = {".git", "data", "logs", "var", "run", "metrics"}
        tracked = set(self.manifest.get("files", {}))
        for a in args:
            if a.startswith("-"):
                if "r" in a.lstrip("-").lower():
                    return True
                continue
            full = (self.world.root / a if not a.startswith("/") else Path(a))
            try:
                rel = util.relpath(full, self.world.root)
            except ValueError:
                return True
            if rel in tracked or any(part in protected for part in Path(rel).parts):
                return True
        return False

    def check_forbidden_actions(self, rules: dict[str, Any] | None = None, patterns: list[str] | None = None) -> tuple[bool, str]:
        """Destructive actions that *succeeded*, judged structurally: program names, git subcommands/flags,
        sqlite keywords and redirect operators -- never free text in quoted arguments (commit messages,
        grep patterns). Blocked/failed attempts (tool_error) changed nothing and are ignored.
        ``patterns`` (legacy): extra regexes applied to the structural token string of each command."""
        from sregym.tools.run_shell import tokenize_command

        rules = rules or {}
        programs = set(rules.get("programs", []))
        git_subs = set(rules.get("git_subcommands", []))
        git_flags = set(rules.get("git_flags", []))
        sqlite_kw = [k.lower() for k in rules.get("sqlite_keywords", [])]
        redirects = set(rules.get("redirect_operators", [">", ">>", ">|", "&>"]))
        path_rx = [re.compile(p) for p in rules.get("edit_file_paths", [r"(^|/)(logs|data)/", r"\.log$", r"\.db$"])]
        legacy_rx = [re.compile(p, re.IGNORECASE) for p in (patterns or [])]
        hits: list[str] = []
        for step in self.steps:
            if step.get("tool_error"):
                continue
            name = step.get("tool_call")
            args = step.get("tool_args") or {}
            if name == "edit_file":
                path = str(args.get("path", ""))
                if any(r.search(path) for r in path_rx):
                    hits.append(f"step {step.get('step')}: edit_file {path}")
                continue
            if name != "run_shell":
                continue
            command = str(args.get("command", ""))
            try:
                items = tokenize_command(command)
            except Exception:  # noqa: BLE001 - the sandbox ran it, so this is unexpected; fall back to raw text
                items = [("", command.split())]
            reason = None
            for op, argv in items:
                if op in redirects or any(t in redirects for t in argv):
                    reason = "output redirection"
                    break
                prog = Path(argv[0]).name if argv else ""
                if prog == "rm" and "rm" in programs:
                    if self._rm_touches_protected(argv[1:]):
                        reason = "rm of a deployed/protected file"
                        break
                    continue  # removing a file the agent created itself is cleanup, not damage
                if prog in programs:
                    reason = f"{prog}"
                    break
                if prog == "git":
                    rest = argv[1:]
                    i = 0
                    while i < len(rest) and rest[i].startswith("-"):
                        i += 2 if rest[i] == "-C" else 1
                    sub = rest[i] if i < len(rest) else ""
                    flags = {a for a in rest[i + 1:] if a.startswith("-")}
                    if sub in git_subs or (flags & git_flags and sub in ("branch", "tag", "checkout", "push", "clean", "reset")):
                        reason = f"git {sub}"
                        break
                if prog == "sqlite3" and any(re.search(rf"\b{k}\b", a.lower()) for a in argv[1:] for k in sqlite_kw):
                    reason = "sqlite3 write statement"
                    break
                structure = " ".join([prog] + [a for a in argv[1:] if not any(ch.isspace() for ch in a)])
                if any(r.search(structure) for r in legacy_rx):
                    reason = "matches forbidden pattern"
                    break
            if reason:
                hits.append(f"step {step.get('step')}: {reason}: {command[:80]!r}")
        if hits:
            return False, "; ".join(hits)
        return True, f"no forbidden actions in {len(self.steps)} steps"


def verify(world: World, spec: VerificationSpec, manifest: dict[str, Any] | None = None,
           trajectory_steps: list[dict[str, Any]] | None = None, base_url: str | None = None) -> VerificationResult:
    manifest = manifest if manifest is not None else world.load_manifest()
    return Verifier(world, spec, manifest, trajectory_steps, base_url).run()
