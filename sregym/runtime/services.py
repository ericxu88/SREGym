"""Process supervisor for the generated service (stand-in for systemd)."""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import time
from datetime import datetime, timezone

from sregym import util
from sregym.generator.world import SERVICE_NAME, World

_MANAGERS: list["ServiceManager"] = []


def _cleanup_all() -> None:
    for m in list(_MANAGERS):
        try:
            m.stop(timeout=3)
        except Exception:  # noqa: BLE001
            pass


atexit.register(_cleanup_all)


class ServiceManager:
    """Start/stop/restart the checkout-service process.

    The process is started with a *clean* environment so the app's ``.env`` is the
    only source of configuration; stdout/stderr are appended to the app log so startup
    crashes are visible where an operator would look.
    """

    def __init__(self, world: World):
        self.world = world
        self.proc: subprocess.Popen | None = None
        self.started_at: datetime | None = None
        self.events: list[str] = []
        _MANAGERS.append(self)

    # ------------------------------------------------------------------ state
    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc and self.proc.poll() is None else None

    def is_running(self) -> bool:
        return self.pid is not None

    def _log_event(self, msg: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {msg}"
        self.events.append(line)
        deploy_log = self.world.log_dir / "deploy.log"
        try:
            with open(deploy_log, "a") as f:
                f.write(f"{stamp} service-manager: {msg}\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ lifecycle
    def start(self, wait: bool = True, timeout: float = 15.0, announce: bool = True) -> str:
        if self.is_running():
            return f"{SERVICE_NAME} is already running (pid {self.pid})"
        world = self.world
        world.log_dir.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SREGYM_WORLD": str(world.root),
        }
        for k in ("SYSTEMROOT", "TMPDIR"):
            if k in os.environ:
                env[k] = os.environ[k]
        log_fh = open(world.app_log, "a")
        try:
            self.proc = subprocess.Popen(
                [world.python, "-m", "checkout.serve"],
                cwd=world.repo, env=env, stdout=log_fh, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_fh.close()
        self.started_at = datetime.now(timezone.utc)
        if announce:
            self._log_event(f"starting {SERVICE_NAME} (pid {self.proc.pid})")
        if not wait:
            return f"{SERVICE_NAME} starting (pid {self.proc.pid})"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return (f"{SERVICE_NAME} exited immediately with code {self.proc.returncode} "
                        f"(see {util.relpath(world.app_log, world.root)})")
            if util.port_open(world.port):
                return f"{SERVICE_NAME} started (pid {self.proc.pid}), listening on 127.0.0.1:{world.port}"
            time.sleep(0.1)
        return f"{SERVICE_NAME} started (pid {self.proc.pid}) but port {world.port} not accepting connections after {timeout:.0f}s"

    def stop(self, timeout: float = 10.0) -> str:
        if not self.proc:
            return f"{SERVICE_NAME} is not running"
        if self.proc.poll() is not None:
            code = self.proc.returncode
            self.proc = None
            return f"{SERVICE_NAME} was not running (last exit code {code})"
        pid = self.proc.pid
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=timeout)
            msg = f"{SERVICE_NAME} stopped (pid {pid})"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=5)
            msg = f"{SERVICE_NAME} did not stop within {timeout:.0f}s; killed (pid {pid})"
        self.proc = None
        self._log_event(msg)
        try:
            self.world.pid_file.unlink()
        except OSError:
            pass
        return msg

    def restart(self, timeout: float = 15.0) -> str:
        first = self.stop()
        time.sleep(0.2)
        second = self.start(timeout=timeout)
        return first + "\n" + second

    def status(self) -> str:
        pid = self.pid
        lines = [f"● {SERVICE_NAME} - checkout-service (FastAPI/uvicorn)"]
        if pid is None:
            lines.append("   Active: inactive (dead)")
            if self.proc is not None and self.proc.returncode is not None:
                lines.append(f"   Last exit code: {self.proc.returncode}")
        else:
            up = (datetime.now(timezone.utc) - self.started_at).total_seconds() if self.started_at else 0
            lines.append(f"   Active: active (running) since {self.started_at:%Y-%m-%d %H:%M:%S} UTC; {int(up)}s ago")
            lines.append(f" Main PID: {pid} (python -m checkout.serve)")
            lines.append(f"   Listen: 127.0.0.1:{self.world.port} ({'open' if util.port_open(self.world.port) else 'not accepting connections'})")
        status, body = util.http_request("GET", f"{self.world.base_url}/health", timeout=3)
        body = body.strip().replace("\n", " ")
        lines.append(f"   Health: GET /health -> {status if status else 'connection failed'} {body[:400]}")
        return "\n".join(lines)

    def close(self) -> None:
        self.stop()
        if self in _MANAGERS:
            _MANAGERS.remove(self)
