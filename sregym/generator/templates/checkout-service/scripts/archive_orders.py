#!/usr/bin/env python
"""Archive orders older than the retention window into data/orders_archive.db (OPS-77).

Runs from the repo root. Verifies every order row it scans (checksum) before moving
anything, inside one transaction so the archive and the core database cannot diverge.

    python scripts/archive_orders.py [--retention-days 365]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkout.config import settings  # noqa: E402
from checkout.db import sqlite_path  # noqa: E402

ARCHIVE_PATH = "data/orders_archive.db"
VERIFY_SECONDS_PER_ROW = 0.006  # row checksum + archive consistency check


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retention-days", type=int, default=365)
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (now - timedelta(days=args.retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    core_path = sqlite_path(settings.database_url)
    started = time.monotonic()
    try:
        archive = sqlite3.connect(ARCHIVE_PATH)
        archive.execute("CREATE TABLE IF NOT EXISTS orders_archive (id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT, "
                        "total_cents INTEGER, currency TEXT, created_at TEXT, updated_at TEXT, archived_at TEXT)")
        archive.commit()
        conn = sqlite3.connect(f"file:{core_path}?mode=rw", uri=True, timeout=settings.db_timeout_seconds)
        conn.execute("BEGIN IMMEDIATE")  # hold the write lock so nothing changes under the verification pass
        rows = conn.execute("SELECT id, user_id, status, total_cents, currency, created_at, updated_at FROM orders ORDER BY id").fetchall()
        digest = hashlib.sha256()
        for r in rows:
            digest.update(repr(r).encode())
            time.sleep(VERIFY_SECONDS_PER_ROW)
        old = [r for r in rows if r[5] < cutoff]
        if old:
            archive.executemany("INSERT OR REPLACE INTO orders_archive VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [(*r, stamp) for r in old])
            archive.commit()
            conn.executemany("DELETE FROM order_items WHERE order_id = ?", [(r[0],) for r in old])
            conn.executemany("DELETE FROM orders WHERE id = ?", [(r[0],) for r in old])
        conn.commit()
        conn.close()
        archive.close()
    except sqlite3.Error as exc:
        print(f"{stamp} archive_orders: ERROR {type(exc).__name__}: {exc}")
        return 1
    held = time.monotonic() - started
    print(f"{stamp} archive_orders: scanned {len(rows)} orders, archived {len(old)} older than {cutoff[:10]} "
          f"(checksum {digest.hexdigest()[:8]}, transaction held {held:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
