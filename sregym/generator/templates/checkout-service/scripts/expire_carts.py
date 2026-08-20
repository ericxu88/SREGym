#!/usr/bin/env python
"""Expire abandoned carts. Run from the repo root by cron every 15 minutes:

    */15 * * * *  cd /srv/__SREGYM_SERVICE__ && python scripts/expire_carts.py >> logs/cron.log 2>&1
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __SREGYM_PKG__.config import settings  # noqa: E402
from __SREGYM_PKG__.db import core_db  # noqa: E402


def main() -> int:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with core_db() as conn:
            scanned = conn.execute("SELECT COUNT(*) FROM carts WHERE status = 'active'").fetchone()[0]
            cur = conn.execute(
                "UPDATE carts SET status = 'expired' WHERE status = 'active' AND expires_at < ?",
                (now.strftime("%Y-%m-%dT%H:%M:%SZ"),),
            )
        print(f"{stamp} expire_carts: scanned {scanned} active carts, expired {cur.rowcount} (ttl={settings.cart_ttl_minutes}m)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"{stamp} expire_carts: ERROR {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
