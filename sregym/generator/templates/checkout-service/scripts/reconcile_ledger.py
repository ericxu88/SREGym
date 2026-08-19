#!/usr/bin/env python
"""Ledger reconciliation for checkout-service.

Compares confirmed orders in the core database with payments in the ledger and reports
orders that have no ledger payment. Optionally copies the missing payments from another
ledger-format database (for example an audit snapshot) into the ledger.

    python scripts/reconcile_ledger.py                          # report missing payments (last 24h)
    python scripts/reconcile_ledger.py --since 2026-08-18T14:00:00Z
    python scripts/reconcile_ledger.py --source data/ledger-snapshot-20260812.db          # show what could be copied
    python scripts/reconcile_ledger.py --source data/ledger-snapshot-20260812.db --apply  # copy missing payments

The target ledger is the one configured in .env (LEDGER_DATABASE_URL) unless --target is given.
Run from the repo root. Exit code 0 = consistent, 2 = missing payments, 1 = error.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkout.config import settings  # noqa: E402
from checkout.db import sqlite_path  # noqa: E402

PAYMENT_COLS = ("order_id", "user_id", "amount_cents", "currency", "method", "status", "gateway_ref", "created_at")


def _open(path: str, readonly: bool = True) -> sqlite3.Connection:
    mode = "ro" if readonly else "rw"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", help="ISO-8601 UTC timestamp or HH:MM (default: 24 hours ago)")
    ap.add_argument("--source", help="ledger-format database to copy missing payments from")
    ap.add_argument("--target", help="ledger database to reconcile/repair (default: LEDGER_DATABASE_URL from .env)")
    ap.add_argument("--apply", action="store_true", help="copy missing payments from --source into the target")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    if not args.since:
        since = now - timedelta(hours=24)
    elif len(args.since) == 5 and args.since[2] == ":":
        since = now.replace(hour=int(args.since[:2]), minute=int(args.since[3:]), second=0, microsecond=0)
        if since > now:
            since -= timedelta(days=1)
    else:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00")).astimezone(timezone.utc)
    since_s = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    core_path = sqlite_path(settings.database_url)
    target_path = args.target or sqlite_path(settings.ledger_database_url)
    print(f"reconcile: core={core_path} target={target_path} since={since_s}")
    try:
        core = _open(core_path)
        target = _open(target_path, readonly=not args.apply)
    except sqlite3.Error as exc:
        print(f"reconcile: ERROR opening database: {exc}")
        return 1

    orders = core.execute(
        "SELECT id, user_id, total_cents, currency, created_at FROM orders WHERE status = 'confirmed' AND created_at >= ? ORDER BY id",
        (since_s,),
    ).fetchall()
    have = {r[0] for r in target.execute("SELECT DISTINCT order_id FROM payments WHERE created_at >= ?", (since_s,))}
    missing = [o for o in orders if o["id"] not in have]
    print(f"reconcile: confirmed orders since {since_s}: {len(orders)}; with ledger payment: {len(orders) - len(missing)}; missing: {len(missing)}")
    if missing:
        ids = [o["id"] for o in missing]
        print(f"reconcile: missing order ids {ids[0]}..{ids[-1]} (earliest {missing[0]['created_at']}, latest {missing[-1]['created_at']})")
    if not args.source:
        return 2 if missing else 0

    try:
        source = _open(args.source)
    except sqlite3.Error as exc:
        print(f"reconcile: ERROR opening source {args.source}: {exc}")
        return 1
    placeholders = ",".join("?" for _ in missing) or "NULL"
    rows = source.execute(
        f"SELECT {', '.join(PAYMENT_COLS)} FROM payments WHERE order_id IN ({placeholders}) ORDER BY id",
        [o["id"] for o in missing],
    ).fetchall() if missing else []
    print(f"reconcile: source {args.source} has payments for {len(rows)} of the {len(missing)} missing orders")
    if not args.apply:
        if rows:
            print("reconcile: re-run with --apply to copy them into the target ledger")
        return 2 if missing else 0
    if not rows:
        return 2 if missing else 0
    target.executemany(
        f"INSERT INTO payments ({', '.join(PAYMENT_COLS)}) VALUES ({', '.join('?' for _ in PAYMENT_COLS)})",
        [tuple(r[c] for c in PAYMENT_COLS) for r in rows],
    )
    target.commit()
    still = len(missing) - len(rows)
    print(f"reconcile: copied {len(rows)} payments into {target_path}" + (f"; {still} orders still have no payment anywhere" if still else "; ledger is complete"))
    return 2 if still else 0


if __name__ == "__main__":
    raise SystemExit(main())
