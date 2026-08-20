"""Process entrypoint: ``python -m checkout.serve``.

Configures logging (file from LOG_PATH or stderr, UTC timestamps) and runs uvicorn.
"""
from __future__ import annotations

import logging
import logging.config
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "lib")  # internal packages installed by scripts/deploy_deps.py (run from the repo root)

import uvicorn

from . import __version__
from .config import report_config, settings

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


def build_log_config() -> dict:
    handler: dict = {"class": "logging.StreamHandler", "formatter": "std", "stream": "ext://sys.stderr"}
    if settings.log_path:
        Path(settings.log_path).parent.mkdir(parents=True, exist_ok=True)
        handler = {"class": "logging.FileHandler", "formatter": "std", "filename": settings.log_path, "encoding": "utf-8"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"std": {"()": UTCFormatter, "format": LOG_FORMAT, "datefmt": LOG_DATEFMT}},
        "handlers": {"default": handler},
        "root": {"level": settings.log_level, "handlers": ["default"]},
        "loggers": {
            "uvicorn.access": {"level": "WARNING", "propagate": False},  # we write our own access log
        },
    }


def main() -> None:
    logging.config.dictConfig(build_log_config())
    log = logging.getLogger("checkout.serve")
    from .main import COMMIT, app  # import after logging is configured

    run_dir = Path("run")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{settings.app_name}.pid").write_text(str(os.getpid()))
    log.info("starting %s %s (commit %s) pid=%d", settings.app_name, __version__, COMMIT, os.getpid())
    report_config(logging.getLogger("checkout.config"))
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, access_log=False)
    log.info("%s stopped", settings.app_name)


if __name__ == "__main__":
    main()
