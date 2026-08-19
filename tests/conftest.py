from __future__ import annotations

from datetime import datetime, timezone

HISTORY_MINUTES = 40  # keep generation fast in tests
FIXED_NOW = datetime(2026, 8, 18, 14, 40, 0, tzinfo=timezone.utc)
