"""SQLite connections for the service databases.

Connections are opened per transaction with ``mode=rw`` so that a missing database
file is an error rather than silently creating an empty database.
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from typing import Iterator

from . import telemetry
from .config import settings

log = logging.getLogger("__SREGYM_PKG__.db")


class ConfigurationError(RuntimeError):
    """Raised when a database URL is not something we can open."""


def sqlite_path(url: str) -> str:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ConfigurationError(f"unsupported database URL (expected sqlite:///...): {url!r}")
    return url[len(prefix):].split("?", 1)[0]


def _connect(url: str) -> sqlite3.Connection:
    path = sqlite_path(url)
    conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=settings.db_timeout_seconds)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _session(url: str, name: str) -> Iterator[sqlite3.Connection]:
    started = time.monotonic()
    try:
        conn = _connect(url)
    except (sqlite3.Error, ConfigurationError):
        telemetry.db_error(name)
        raise
    try:
        yield conn
        conn.commit()
    except sqlite3.Error:
        telemetry.db_error(name)
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        elapsed_ms = (time.monotonic() - started) * 1000
        if elapsed_ms > settings.slow_query_ms:
            log.warning("slow database transaction (%dms) db=%s", elapsed_ms, name)


@contextlib.contextmanager
def core_db() -> Iterator[sqlite3.Connection]:
    """Users, products, orders, carts."""
    yield from _session(settings.database_url, "core")


#[[ ledger
@contextlib.contextmanager
def ledger_db() -> Iterator[sqlite3.Connection]:
    """Payments ledger (kept in a separate database file for audit isolation)."""
    yield from _session(settings.ledger_database_url, "ledger")


#]] ledger
def ping(url: str) -> None:
    conn = _connect(url)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
