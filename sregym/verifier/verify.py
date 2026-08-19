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

    def check_path_exists(self, path: str) -> tuple[bool, str]:
        p = self.world.root / path
        return (True, f"{path} present") if p.exists() else (False, f"{path} is missing")

    # ------------------------------------------------------------------ collateral checks
    def check_manifest_files_unchanged(self, allow: list[str] | None = None) -> tuple[bool, str]:
        allow_set = set(allow or [])
        current = self.world.file_hashes()
        before = self.manifest["files"]
        modified = sorted(k for k in before if k in current and current[k] != before[k] and k not in allow_set)
        deleted = sorted(k for k in before if k not in current and k not in allow_set)
        created = sorted(k for k in current if k not in before and k not in allow_set)
        problems = []
        if modified:
            problems.append("modified: " + ", ".join(modified))
        if deleted:
            problems.append("deleted: " + ", ".join(deleted))
        if created:
            problems.append("created: " + ", ".join(created))
        if problems:
            return False, "; ".join(problems)
        return True, f"{len(before)} tracked files intact (allowed to change: {', '.join(sorted(allow_set)) or '-'})"

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
                schema = util.sha256_text("\n".join(
                    r[0] or "" for r in conn.execute("SELECT sql FROM sqlite_master WHERE type IN ('table','index') ORDER BY name")))
                if schema != snap["__schema__"]["hash"]:
                    problems.append(f"{rel}: schema changed")
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
                    elif db_rows_hash(conn, table, info["max_rowid"]) != info["hash"]:
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
            elif util.head_hash(p) != info["head_hash"]:
                problems.append(f"{rel} rewritten (head changed)")
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
