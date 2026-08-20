"""Process supervisor for the generated service (stand-in for systemd)."""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import time
from datetime import datetime, timezone

from sregym import util
from sregym.generator.world import World

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
        self.svc = world.naming.service
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
    START_LIMIT = 5  # like systemd: Restart=on-failure with a burst limit, then give up

    def start(self, wait: bool = True, timeout: float = 15.0, announce: bool = True) -> str:
        """Start the service; if it exits immediately, retry up to START_LIMIT times (crash-loop), then fail."""
        if self.is_running():
            return f"{self.svc} is already running (pid {self.pid})"
        last = ""
        for attempt in range(1, self.START_LIMIT + 1):
            last = self._start_once(wait=wait, timeout=timeout, announce=announce and attempt == 1)
            if "exited immediately" not in last:
                if attempt > 1:
                    last += f" (after {attempt} attempts)"
                return last
            self._log_event(f"{self.svc} exited with code {self.proc.returncode if self.proc else '?'} "
                            f"(attempt {attempt}/{self.START_LIMIT}); restarting in 2s")
            time.sleep(0.4)
        self._log_event(f"{self.svc}: start limit exceeded ({self.START_LIMIT} rapid failures); giving up (Result: start-limit-hit)")
        return (f"{self.svc} failed to start: crashed {self.START_LIMIT} times in a row "
                f"(last exit code {self.proc.returncode if self.proc else '?'}); start limit exceeded. "
                f"See {util.relpath(self.world.app_log, self.world.root)} for the crash output.")

    def _start_once(self, wait: bool = True, timeout: float = 15.0, announce: bool = True) -> str:
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
                [world.python, "-m", f"{world.naming.package}.serve"],
                cwd=world.repo, env=env, stdout=log_fh, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_fh.close()
        self.started_at = datetime.now(timezone.utc)
        if announce:
            self._log_event(f"starting {self.svc} (pid {self.proc.pid})")
        if not wait:
            return f"{self.svc} starting (pid {self.proc.pid})"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                time.sleep(0.15)  # let the crash output finish flushing to the log
                return (f"{self.svc} exited immediately with code {self.proc.returncode} "
                        f"(see {util.relpath(world.app_log, world.root)})")
            if util.port_open(world.port):
                return f"{self.svc} started (pid {self.proc.pid}), listening on 127.0.0.1:{world.port}"
            time.sleep(0.1)
        return f"{self.svc} started (pid {self.proc.pid}) but port {world.port} not accepting connections after {timeout:.0f}s"

    def stop(self, timeout: float = 10.0) -> str:
        if not self.proc:
            return f"{self.svc} is not running"
        if self.proc.poll() is not None:
            code = self.proc.returncode
            self.proc = None
            return f"{self.svc} was not running (last exit code {code})"
        pid = self.proc.pid
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=timeout)
            msg = f"{self.svc} stopped (pid {pid})"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=5)
            msg = f"{self.svc} did not stop within {timeout:.0f}s; killed (pid {pid})"
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
        lines = [f"● {self.svc} - {self.svc} (FastAPI/uvicorn)"]
        if pid is None:
            if self.proc is not None and self.proc.returncode not in (None, 0, -15):
                lines.append("   Active: failed (Result: start-limit-hit)" if self.events and "start limit" in self.events[-1]
                             else "   Active: failed")
                lines.append(f"   Last exit code: {self.proc.returncode}")
            else:
                lines.append("   Active: inactive (dead)")
        else:
            up = (datetime.now(timezone.utc) - self.started_at).total_seconds() if self.started_at else 0
            lines.append(f"   Active: active (running) since {self.started_at:%Y-%m-%d %H:%M:%S} UTC; {int(up)}s ago")
            lines.append(f" Main PID: {pid} (python -m {self.world.naming.package}.serve)")
            lines.append(f"   Listen: 127.0.0.1:{self.world.port} ({'open' if util.port_open(self.world.port) else 'not accepting connections'})")
        status, body = util.http_request("GET", f"{self.world.base_url}/health", timeout=3)
        body = body.strip().replace("\n", " ")
        lines.append(f"   Health: GET /health -> {status if status else 'connection failed'} {body[:400]}")
        return "\n".join(lines)

    def close(self) -> None:
        self.stop()
        if self in _MANAGERS:
            _MANAGERS.remove(self)
