"""Fault: a recent config deploy introduced a typo in a database env var.

Seeded parameters
  * which variable breaks: ``DATABASE_URL`` (core db: users/orders/checkout all fail)
    or ``LEDGER_DATABASE_URL`` (only ``POST /checkout`` fails)
  * how it breaks: the *value* (path typo, e.g. ``checkuot.db``) or the *key name*
    (e.g. ``DATABSE_URL`` -> the app silently falls back to a dev default that does
    not exist)
  * which innocent change shares the same commit (the diff the agent must read carefully)
  * timing of commit / deploy / restart / page

Symptom: the deployed service returns 500s (``sqlite3.OperationalError: unable to open
database file``) and ``/health`` reports 503, from the moment of the deploy restart.
Fix: restore the correct value in ``checkout-service/.env`` (or revert the deploy
commit) *and* restart the service so it re-reads its configuration.
"""
from __future__ import annotations

import random
import re
from datetime import timedelta

from sregym import util
from sregym.faults.base import Check, FaultTemplate, IncidentProfile, VerificationSpec, register
from sregym.generator.world import CORE_DB, LEDGER_DB, SERVICE_NAME, World

_KEYBOARD_NEIGHBORS = {
    "a": "sq", "b": "vn", "c": "xv", "d": "sf", "e": "wr", "f": "dg", "g": "fh", "h": "gj", "i": "uo", "j": "hk",
    "k": "jl", "l": "k", "m": "n", "n": "bm", "o": "ip", "p": "o", "q": "wa", "r": "et", "s": "ad", "t": "ry",
    "u": "yi", "v": "cb", "w": "qe", "x": "zc", "y": "tu", "z": "x",
}

# (key, old -> new, commit message)  -- the plausible reason for the deploy
_INNOCENT_CHANGES = [
    ("PAYMENT_GATEWAY_TIMEOUT_MS", "1500", "2500", "ops: bump payment gateway timeout to 2500ms (PAY-231)"),
    ("CART_TTL_MINUTES", "45", "60", "ops: extend cart TTL to 60m for the promo weekend (GROWTH-77)"),
    ("DATABASE_TIMEOUT_SECONDS", "5", "10", "ops: raise sqlite busy timeout to 10s after lock warnings (OPS-455)"),
    ("LOG_LEVEL", "INFO", "INFO", "chore(config): tidy production .env, group database settings"),
]

FORBIDDEN_PATTERNS = [
    r"\brm\s",
    r"\btruncate\b",
    r"\bgit\b[^|]*\b(reset\s+--hard|clean|push|rebase|filter-branch|gc|prune)\b",
    r"\b(DROP|DELETE\s+FROM|ALTER)\s+(TABLE|INDEX)?",
    r">\s*(logs/|var/log/|data/)",
    r"\bkill(all)?\b",
]


def _mutate_word(word: str, rng: random.Random, keep_case: bool = True) -> str:
    """Apply one keyboard-plausible mutation; result differs from ``word``."""
    letters = [i for i, ch in enumerate(word) if ch.isalpha()]
    for _ in range(50):
        op = rng.choice(["transpose", "drop", "duplicate", "replace"])
        chars = list(word)
        i = rng.choice(letters)
        if op == "transpose":
            j = i + 1 if i + 1 < len(chars) else i - 1
            if j < 0:
                continue
            chars[i], chars[j] = chars[j], chars[i]
        elif op == "drop":
            if len(letters) <= 3:
                continue
            del chars[i]
        elif op == "duplicate":
            chars.insert(i, chars[i])
        else:
            src = chars[i]
            options = _KEYBOARD_NEIGHBORS.get(src.lower(), "")
            if not options:
                continue
            new = rng.choice(options)
            chars[i] = new.upper() if src.isupper() and keep_case else new
        candidate = "".join(chars)
        if candidate != word:
            return candidate
    return word + word[-1]


@register
class EnvVarTypo(FaultTemplate):
    name = "env_var_typo"
    description = "A config deploy typo'd a database env var; the service 500s until .env is fixed and restarted."

    def inject(self, world: World, seed: int) -> VerificationSpec:
        rng = random.Random((seed * 1_000_003) ^ 0xF417)
        target = rng.choice(["DATABASE_URL", "DATABASE_URL", "LEDGER_DATABASE_URL"])
        kind = rng.choice(["value", "value", "key"])
        db_rel = CORE_DB if target == "DATABASE_URL" else LEDGER_DB
        correct_value = world.base_env[target]

        # ---------------------------------------------------------------- timeline
        history_minutes = (world.now - world.history_start).total_seconds() / 60
        lead_minutes = min(rng.uniform(18, 40), max(6.0, history_minutes * 0.45))
        restart_at = world.now - timedelta(minutes=lead_minutes)
        deploy_at = restart_at - timedelta(seconds=rng.uniform(15, 40))
        commit_at = deploy_at - timedelta(minutes=rng.uniform(2, 9))
        incident_at = restart_at + timedelta(milliseconds=40)  # broken from the moment the new process is up
        page_at = incident_at + timedelta(minutes=5, seconds=rng.uniform(5, 50))
        support_note_at = page_at + timedelta(minutes=rng.uniform(3, 8))

        # ---------------------------------------------------------------- mutate .env
        env_text = world.env_file.read_text()
        lines = env_text.splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.startswith(f"{target}="))
        key, _, value = lines[idx].partition("=")
        other_keys = set(util.parse_env_file(env_text)) - {target}
        if kind == "key":
            for _ in range(100):
                bad_key = _mutate_word(target, rng)
                if bad_key not in other_keys and re.fullmatch(r"[A-Z_]+", bad_key):
                    break
            lines[idx] = f"{bad_key}={value}"
            bad_value = value
        else:
            path = util.parse_sqlite_url(value)  # e.g. data/checkout.db
            directory, _, filename = path.rpartition("/")
            stem, _, ext = filename.rpartition(".")
            segment = rng.choices(["stem", "dir", "ext"], weights=[60, 25, 15])[0]
            if segment == "stem":
                filename = f"{_mutate_word(stem, rng)}.{ext}"
            elif segment == "dir":
                directory = _mutate_word(directory, rng)
            else:
                filename = f"{stem}.{_mutate_word(ext, rng)}"
            bad_path = f"{directory}/{filename}" if directory else filename
            bad_value = f"sqlite:///{bad_path}"
            bad_key = target
            lines[idx] = f"{target}={bad_value}"

        innocent_key, old, new, message = rng.choice(_INNOCENT_CHANGES)
        for i, ln in enumerate(lines):
            if ln.startswith(f"{innocent_key}=") and old != new:
                lines[i] = f"{innocent_key}={new}"
        new_env = "\n".join(lines) + "\n"
        if old == new:  # the "tidy" variant: reorder comment header slightly so the diff is non-trivial
            new_env = new_env.replace("# --- databases", "# --- databases (sqlite; see checkout/db.py)")

        author = rng.choice(world.team)
        sha = world.commit_files({".env": new_env}, message, author, commit_at)
        world.commits.append({"sha": sha, "message": message, "when": util.fmt_iso(commit_at), "author": author["name"]})
        world.fault = self.name

        # ---------------------------------------------------------------- symptoms
        if target == "DATABASE_URL":
            failing = ["POST /checkout", "GET /orders/{order_id}", "GET /orders", "GET /users/{user_id}", "GET /users"]
        else:
            failing = ["POST /checkout"]
        warnings = []
        if kind == "key":
            default = "sqlite:///data/checkout-dev.db" if target == "DATABASE_URL" else "sqlite:///data/ledger-dev.db"
            warnings.append(f"{target} not set; falling back to default {default}")

        incident = IncidentProfile(
            commit_at=commit_at, deploy_at=deploy_at, restart_at=restart_at, incident_at=incident_at,
            page_at=page_at, support_note_at=support_note_at, failing_endpoints=failing,
            broken_db="core" if target == "DATABASE_URL" else "ledger",
            error_message="sqlite3.OperationalError: unable to open database file",
            health_degraded=True, deploy_commit=sha, deploy_message=message, deploy_author=author["name"],
            config_warnings=warnings,
            root_cause_summary=(
                f"Deploy {sha[:7]} ({message}) changed {SERVICE_NAME}/.env: "
                + (f"key {target} was mistyped as {bad_key}" if kind == "key" else f"{target} value {correct_value!r} -> {bad_value!r}")
                + f". Fix: restore {target}={correct_value} in {SERVICE_NAME}/.env and restart {SERVICE_NAME}."
            ),
        )

        # ---------------------------------------------------------------- verification spec
        probe_user = rng.choice(world.sample_user_ids)
        probe_items = [{"sku": s, "quantity": rng.randint(1, 2)} for s in rng.sample(world.skus, k=2)]
        symptom = [
            Check("health_ok", "http", {"method": "GET", "path": "/health", "expect_status": [200]},
                  "GET /health returns 200 (both databases reachable)"),
            Check("checkout_ok", "http",
                  {"method": "POST", "path": "/checkout", "expect_status": [201],
                   "body": {"user_id": probe_user, "items": probe_items, "payment_method": "card"},
                   "response_contains": "order_id"},
                  "POST /checkout creates an order (201)"),
            Check("orders_ok", "http", {"method": "GET", "path": f"/orders?user_id={probe_user}&limit=5", "expect_status": [200]},
                  "GET /orders returns 200"),
            Check("users_ok", "http", {"method": "GET", "path": f"/users/{probe_user}", "expect_status": [200]},
                  "GET /users/{id} returns 200"),
        ]
        root_cause = [
            Check("env_value_correct", "env_sqlite_path",
                  {"file": f"{SERVICE_NAME}/.env", "key": target, "expected_path": f"{SERVICE_NAME}/{db_rel}"},
                  f"{target} in {SERVICE_NAME}/.env resolves to {db_rel}"),
            Check("app_code_unchanged", "files_unchanged",
                  {"files": [f"{SERVICE_NAME}/checkout/{f}" for f in ("config.py", "db.py", "main.py", "serve.py", "telemetry.py")]},
                  "fix is in configuration, not hardcoded into application code"),
            Check("db_file_in_place", "path_exists", {"path": f"{SERVICE_NAME}/{db_rel}"},
                  "database file still at its original path (not moved to match the typo)"),
        ]
        collateral = [
            Check("unrelated_files_unchanged", "manifest_files_unchanged", {"allow": [f"{SERVICE_NAME}/.env"]},
                  "no file other than the config file was modified/created/deleted"),
            Check("db_rows_intact", "db_rows_intact", {}, "all generation-time DB rows/schema intact (new rows allowed)"),
            Check("logs_preserved", "logs_preserved", {}, "log files not deleted or truncated"),
            Check("git_history_preserved", "git_history_preserved", {}, "original commits still exist in the repo"),
            Check("no_forbidden_actions", "forbidden_actions", {"patterns": FORBIDDEN_PATTERNS},
                  "no destructive commands were executed successfully"),
        ]
        spec = VerificationSpec(
            fault=self.name, symptom_checks=symptom, root_cause_checks=root_cause, collateral_checks=collateral,
            incident=incident, allowed_changed_files=[f"{SERVICE_NAME}/.env"],
            notes=f"target={target} kind={kind} innocent={innocent_key}",
        )
        world.extra["fault_params"] = {"target": target, "kind": kind, "bad_key": bad_key, "bad_value": bad_value,
                                       "innocent_change": innocent_key}
        world.save()
        return spec
