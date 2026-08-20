"""Configuration for __SREGYM_SERVICE__.

Settings are loaded once, at import time, from the ``.env`` file in the working
directory (override the location with ``CHECKOUT_ENV_FILE``).  Real environment
variables take precedence over the file (12-factor).  Anything not provided falls
back to the development defaults below.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = Path(os.environ.get("CHECKOUT_ENV_FILE", ".env"))

DEFAULTS: dict[str, str] = {
    "APP_NAME": "__SREGYM_SERVICE__",
    "APP_ENV": "development",
    "APP_HOST": "127.0.0.1",
    "APP_PORT": "8000",
    "DATABASE_URL": "sqlite:///data/__SREGYM_PKG__-dev.db",
    #[[ ledger
    "LEDGER_DATABASE_URL": "sqlite:///data/ledger-dev.db",
    #]] ledger
    "DATABASE_TIMEOUT_SECONDS": "5",
    "DATABASE_MAX_PAGES": "0",
    "SLOW_QUERY_MS": "500",
    #[[ checkout
    "PAYMENT_GATEWAY_URL": "https://sandbox.payments.example.com/v2",
    "PAYMENT_GATEWAY_TIMEOUT_MS": "1500",
    "PAYMENT_GATEWAY_MODE": "stub",
    "CART_TTL_MINUTES": "45",
    #]] checkout
    "LOG_PATH": "",
    "LOG_LEVEL": "INFO",
    "RATE_LIMIT_PER_MINUTE": "300",
    "SESSION_SECRET": "dev-secret-change-me",
}


def _parse_env_file(text: str) -> dict[str, str]:
    """KEY=VALUE per line; later keys win; surrounding quotes stripped; # comments ignored."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_env: str
    host: str
    port: int
    database_url: str
    #[[ ledger
    ledger_database_url: str
    #]] ledger
    db_timeout_seconds: float
    database_max_pages: int
    slow_query_ms: int
    #[[ checkout
    payment_gateway_url: str
    payment_gateway_timeout_ms: int
    payment_gateway_mode: str
    cart_ttl_minutes: int
    #]] checkout
    log_path: str
    log_level: str
    rate_limit_per_minute: int
    session_secret: str


def _load() -> tuple[Settings, dict[str, str], list[str]]:
    values = dict(DEFAULTS)
    file_values: dict[str, str] = {}
    if ENV_FILE.exists():
        file_values = _parse_env_file(ENV_FILE.read_text())
        values.update(file_values)
    for key in DEFAULTS:
        if key in os.environ:
            values[key] = os.environ[key]
    # keys that production is expected to set explicitly
    required = ["DATABASE_URL"]
    #[[ ledger
    required.append("LEDGER_DATABASE_URL")
    #]] ledger
    missing = [k for k in required if k not in file_values and k not in os.environ]
    settings = Settings(
        app_name=values["APP_NAME"],
        app_env=values["APP_ENV"],
        host=values["APP_HOST"],
        port=int(values["APP_PORT"]),
        database_url=values["DATABASE_URL"],
        #[[ ledger
        ledger_database_url=values["LEDGER_DATABASE_URL"],
        #]] ledger
        db_timeout_seconds=float(values["DATABASE_TIMEOUT_SECONDS"]),
        database_max_pages=int(values["DATABASE_MAX_PAGES"] or 0),
        slow_query_ms=int(values["SLOW_QUERY_MS"]),
        #[[ checkout
        payment_gateway_url=values["PAYMENT_GATEWAY_URL"],
        payment_gateway_timeout_ms=int(values["PAYMENT_GATEWAY_TIMEOUT_MS"]),
        payment_gateway_mode=values["PAYMENT_GATEWAY_MODE"],
        cart_ttl_minutes=int(values["CART_TTL_MINUTES"]),
        #]] checkout
        log_path=values["LOG_PATH"],
        log_level=values["LOG_LEVEL"].upper(),
        rate_limit_per_minute=int(values["RATE_LIMIT_PER_MINUTE"]),
        session_secret=values["SESSION_SECRET"],
    )
    return settings, file_values, missing


settings, _file_values, _missing_keys = _load()


def report_config(log: logging.Logger) -> None:
    """Log where configuration came from. Called by serve.py once logging is set up."""
    if ENV_FILE.exists():
        log.info("loaded configuration from %s (%d keys)", ENV_FILE, len(_file_values))
    else:
        log.warning("env file %s not found; using defaults", ENV_FILE)
    for key in _missing_keys:
        log.warning("%s not set; falling back to default %s", key, DEFAULTS[key])
    log.info("environment=%s log_level=%s", settings.app_env, settings.log_level)
