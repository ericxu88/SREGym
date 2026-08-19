#!/usr/bin/env python
"""Apply pending schema migrations.

    python scripts/migrate.py            # show applied / pending migrations
    python scripts/migrate.py --apply    # apply pending migrations in order

Migrations live in migrations/NNN_name.sql; files whose name contains "_ledger" target the
payments ledger, everything else targets the core database (paths from .env). Each file
records itself in schema_migrations. Run from the repo root.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkout.config import settings  # noqa: E402
from checkout.db import sqlite_path  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _target(name: str) -> tuple[str, str]:
    if "_ledger" in name:
        return "ledger", sqlite_path(settings.ledger_database_url)
    return "core", sqlite_path(settings.database_url)


def _applied(path: str) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
            return set()
        return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="apply pending schema migrations")
    ap.add_argument("--apply", action="store_true", help="apply pending migrations (default: just show status)")
    args = ap.parse_args()
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("migrate: no migration files found")
        return 1
    pending = []
    for f in files:
        version = f.stem
        db, path = _target(f.name)
        try:
            done = version in _applied(path)
        except sqlite3.Error as exc:
            print(f"migrate: ERROR opening {db} database {path}: {exc}")
            return 1
        print(f"migrate: {version:<28} {db:<6} {path:<28} {'applied' if done else 'PENDING'}")
        if not done:
            pending.append((version, db, path, f))
    if not pending:
        print("migrate: database schema is up to date")
        return 0
    if not args.apply:
        print(f"migrate: {len(pending)} pending migration(s); re-run with --apply to apply them")
        return 2
    for version, db, path, f in pending:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True)
        try:
            conn.executescript(f.read_text())
            conn.commit()
        except sqlite3.Error as exc:
            print(f"migrate: ERROR applying {version} to {db}: {exc}")
            return 1
        finally:
            conn.close()
        print(f"migrate: applied {version} to {db} ({path})")
    print("migrate: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
