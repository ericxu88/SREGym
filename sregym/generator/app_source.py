"""Render the checkout-service application source from templates.

Templates live in ``templates/checkout-service``. They contain *section markers*::

    #[[ ledger
    ... lines only present once the ledger feature exists ...
    #]] ledger

which lets us render earlier revisions of the same file and build a plausible git
history (each feature commit adds its section). Placeholders of the form
``__SREGYM_NAME__`` are substituted with per-world values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / "templates"
APP_TEMPLATE_DIR = TEMPLATES_DIR / "checkout-service"
SYSTEM_TEMPLATE_DIR = TEMPLATES_DIR / "system"

ALL_SECTIONS = frozenset({"checkout", "ledger", "metrics", "cron", "runbook"})

# template relative path -> repo relative path
_APP_FILES = {
    "gitignore": ".gitignore",
    "README.md": "README.md",
    "requirements.txt": "requirements.txt",
    "checkout/__init__.py": "checkout/__init__.py",
    "checkout/config.py": "checkout/config.py",
    "checkout/telemetry.py": "checkout/telemetry.py",
    "checkout/db.py": "checkout/db.py",
    "checkout/main.py": "checkout/main.py",
    "checkout/serve.py": "checkout/serve.py",
    "migrations/001_init.sql": "migrations/001_init.sql",
}
# files that only exist once a section exists
_SECTION_FILES = {
    "ledger": {"migrations/002_ledger.sql": "migrations/002_ledger.sql"},
    "cron": {"scripts/expire_carts.py": "scripts/expire_carts.py"},
}


def render_sections(text: str, include: frozenset[str] | set[str]) -> str:
    """Drop lines inside sections that are not in ``include``; strip marker lines."""
    out: list[str] = []
    stack: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("#[[ "):
            stack.append(stripped[4:].strip())
            continue
        if stripped.startswith("#]] "):
            name = stripped[4:].strip()
            if not stack or stack[-1] != name:
                raise ValueError(f"unbalanced section marker {name!r}")
            stack.pop()
            continue
        if all(name in include for name in stack):
            out.append(line)
    if stack:
        raise ValueError(f"unclosed section(s): {stack}")
    return "".join(out)


def substitute(text: str, values: dict[str, str]) -> str:
    for key, val in values.items():
        text = text.replace(f"__SREGYM_{key}__", str(val))
    return text


def render_app_files(sections: frozenset[str] | set[str], values: dict[str, str]) -> dict[str, str]:
    """Return {repo_relpath: content} for the application at a given feature level."""
    files: dict[str, str] = {}
    mapping = dict(_APP_FILES)
    for sec, extra in _SECTION_FILES.items():
        if sec in sections:
            mapping.update(extra)
    for tmpl_rel, repo_rel in mapping.items():
        text = (APP_TEMPLATE_DIR / tmpl_rel).read_text()
        files[repo_rel] = substitute(render_sections(text, sections), values)
    return files


def render_system_file(name: str, values: dict[str, str]) -> str:
    return substitute((SYSTEM_TEMPLATE_DIR / name).read_text(), values)


# --------------------------------------------------------------------------- .env
# (comment header, [keys...]) -- keys missing from the state are skipped.
ENV_LAYOUT: list[tuple[str, list[str]]] = [
    ("# --- application", ["APP_NAME", "APP_ENV", "APP_HOST", "APP_PORT"]),
    ("# --- databases", ["DATABASE_URL", "LEDGER_DATABASE_URL", "DATABASE_TIMEOUT_SECONDS"]),
    ("# --- payments", ["PAYMENT_GATEWAY_URL", "PAYMENT_GATEWAY_TIMEOUT_MS", "PAYMENT_GATEWAY_MODE", "CART_TTL_MINUTES"]),
    ("# --- logging", ["LOG_PATH", "LOG_LEVEL"]),
    ("# --- limits", ["RATE_LIMIT_PER_MINUTE"]),
    ("# --- secrets (rotate quarterly)", ["SESSION_SECRET"]),
]

ENV_HEADER = (
    "# checkout-service -- production configuration\n"
    "# Managed in git; deploy-bot ships this file to prod hosts and restarts the service.\n"
)


def render_env(state: dict[str, str]) -> str:
    parts = [ENV_HEADER]
    for header, keys in ENV_LAYOUT:
        present = [k for k in keys if k in state]
        if not present:
            continue
        parts.append("\n" + header + "\n")
        for k in present:
            parts.append(f"{k}={state[k]}\n")
    return "".join(parts)


# --------------------------------------------------------------------------- history
@dataclass
class Revision:
    message: str
    version: str
    sections: frozenset[str]
    env: dict[str, str]
    when: datetime
    author_index: int  # index into the world's dev team
    extra_files: dict[str, str] = field(default_factory=dict)


def plan_revisions(*, now: datetime, base_env: dict[str, str], old_secret: str, rng) -> list[Revision]:
    """A believable commit history for the repo, oldest first, ending at ``now``-ish.

    ``base_env`` is the *final, correct* production configuration. Earlier revisions
    are derived from it.
    """
    def ago(days: float, hours: float = 0.0) -> datetime:
        return now - timedelta(days=days, hours=hours)

    env_v1 = {k: v for k, v in base_env.items() if k in {
        "APP_NAME", "APP_ENV", "APP_HOST", "APP_PORT", "DATABASE_URL", "DATABASE_TIMEOUT_SECONDS",
        "LOG_PATH", "LOG_LEVEL", "RATE_LIMIT_PER_MINUTE", "SESSION_SECRET"}}
    env_v1["RATE_LIMIT_PER_MINUTE"] = "300"
    env_v1["SESSION_SECRET"] = old_secret

    env_v2 = dict(env_v1)
    for k in ("PAYMENT_GATEWAY_URL", "PAYMENT_GATEWAY_TIMEOUT_MS", "PAYMENT_GATEWAY_MODE", "CART_TTL_MINUTES"):
        env_v2[k] = base_env[k]

    env_v3 = dict(env_v2)
    env_v3["LEDGER_DATABASE_URL"] = base_env["LEDGER_DATABASE_URL"]

    env_v5 = dict(env_v3)
    env_v5["RATE_LIMIT_PER_MINUTE"] = base_env["RATE_LIMIT_PER_MINUTE"]

    env_v7 = dict(env_v5)
    env_v7["SESSION_SECRET"] = base_env["SESSION_SECRET"]

    j = rng.uniform  # jitter helper
    revisions = [
        Revision("Initial import of checkout-service (users, orders, health)", "1.0.0",
                 frozenset(), env_v1, ago(88 + j(0, 6), j(0, 20)), 0),
        Revision("feat: POST /checkout with stubbed payment authorization", "1.1.0",
                 frozenset({"checkout"}), env_v2, ago(74 + j(0, 6), j(0, 20)), 1),
        Revision("feat(payments): record captured payments in a separate ledger database\n\n"
                 "Audit asked for payment records to live in their own file so they can be\n"
                 "snapshotted independently of the core db. Adds LEDGER_DATABASE_URL and\n"
                 "migrations/002_ledger.sql.", "1.2.0",
                 frozenset({"checkout", "ledger"}), env_v3, ago(58 + j(0, 6), j(0, 20)), 0),
        Revision("feat: expose /metrics for prometheus scraping", "1.3.0",
                 frozenset({"checkout", "ledger", "metrics"}), env_v3, ago(44 + j(0, 5), j(0, 20)), 2),
        Revision("ops: raise RATE_LIMIT_PER_MINUTE 300 -> 600 after LB change (OPS-412)", "1.3.0",
                 frozenset({"checkout", "ledger", "metrics"}), env_v5, ago(31 + j(0, 4), j(0, 20)), 1),
        Revision("feat: cart expiry job (scripts/expire_carts.py) + cron docs", "1.4.0",
                 frozenset({"checkout", "ledger", "metrics", "cron"}), env_v5, ago(19 + j(0, 4), j(0, 20)), 2),
        Revision("chore: rotate SESSION_SECRET", "1.4.0",
                 frozenset({"checkout", "ledger", "metrics", "cron"}), env_v7, ago(9 + j(0, 3), j(0, 20)), 0),
        Revision("docs: on-call notes in README; pin pydantic>=2", "1.4.1",
                 ALL_SECTIONS, env_v7, ago(3 + j(0, 2), j(0, 20)), 1),
    ]
    return revisions
