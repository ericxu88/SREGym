"""A minimal cron daemon for the live world: runs the jobs in etc/cron.d/checkout-service on schedule.

Safety: cron.d is agent-editable, so only commands of the form
``cd <repo> && <python> scripts/<name>.py [args] [>> logs/cron.log 2>&1]`` are executed, and only when the
script is byte-identical to the generation-time manifest (the "deployed" version) -- like the shell tool.
Anything else is logged as skipped. Output is appended to the repo's logs/cron.log.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sregym import util
from sregym.generator.world import SERVICE_NAME, World

_FIELD_RE = re.compile(r"^(\*|\d+)(?:-(\d+))?(?:/(\d+))?$")


def field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    for part in field.split(","):
        m = _FIELD_RE.match(part.strip())
        if not m:
            return False
        base, end, step = m.group(1), m.group(2), m.group(3)
        step_n = int(step) if step else 1
        if base == "*":
            start, stop = lo, hi
        else:
            start = int(base)
            stop = int(end) if end else (hi if step else start)
        if start <= value <= stop and (value - start) % step_n == 0:
            return True
    return False


def schedule_matches(fields: list[str], when: datetime) -> bool:
    minute, hour, dom, mon, dow = fields
    return (field_matches(minute, when.minute, 0, 59) and field_matches(hour, when.hour, 0, 23)
            and field_matches(dom, when.day, 1, 31) and field_matches(mon, when.month, 1, 12)
            and field_matches(dow, when.isoweekday() % 7, 0, 7))


def parse_crontab(text: str) -> list[dict]:
    """Return [{'schedule': [...5 fields], 'user': str, 'command': str, 'line': str}] for active entries."""
    jobs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        jobs.append({"schedule": parts[:5], "user": parts[5], "command": parts[6].split("  #")[0].strip(), "line": raw})
    return jobs


class CronRunner(threading.Thread):
    def __init__(self, world: World, allowed_scripts: dict[str, str] | None = None):
        super().__init__(name="sregym-cron", daemon=True)
        self.world = world
        self.crontab = world.root / "etc" / "cron.d" / SERVICE_NAME
        self.cron_log = world.log_dir / "cron.log"
        self._stop = threading.Event()
        self._allowed = allowed_scripts
        self.runs: list[tuple[datetime, str, int]] = []
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    @property
    def allowed_scripts(self) -> dict[str, str]:
        if self._allowed is None:
            try:
                files = self.world.load_manifest().get("files", {})
            except (OSError, ValueError):
                files = {}
            self._allowed = {rel: sha for rel, sha in files.items() if rel.startswith(f"{SERVICE_NAME}/scripts/") and rel.endswith(".py")}
        return self._allowed

    def busy(self) -> bool:
        with self._lock:
            return bool(self._running)

    def run(self) -> None:
        last_minute = None
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            minute = now.replace(second=0, microsecond=0)
            if minute != last_minute and now.second >= 1:
                last_minute = minute
                try:
                    self._tick(minute)
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(0.5)

    def _tick(self, minute: datetime) -> None:
        if not self.crontab.exists():
            return
        for job in parse_crontab(self.crontab.read_text()):
            if schedule_matches(job["schedule"], minute):
                threading.Thread(target=self._run_job, args=(job, minute), daemon=True).start()

    def _log(self, msg: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.cron_log, "a") as f:
                f.write(f"{stamp} {msg}\n")
        except OSError:
            pass

    def _run_job(self, job: dict, minute: datetime) -> None:
        argv = self._validate(job["command"])
        if argv is None:
            return  # non-python entries (find/sqlite3 maintenance...) are outside this runner; silence is realistic
        script = argv[1]
        full = (self.world.repo / script).resolve()
        rel = util.relpath(full, self.world.root)
        expected = self.allowed_scripts.get(rel)
        if expected is None or not full.exists() or util.sha256_file(full) != expected:
            self._log(f"crond: skipped {script} (not the deployed version of the script)")
            return
        key = script
        with self._lock:
            if key in self._running:
                self._log(f"crond: {script} still running from the previous minute; skipped")
                return
            self._running.add(key)
        started = time.time()
        try:
            env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
            with open(self.cron_log, "a") as out:
                proc = subprocess.run([self.world.python, *argv[1:]], cwd=self.world.repo, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=110)
            self.runs.append((minute, script, proc.returncode))
        except subprocess.TimeoutExpired:
            self._log(f"crond: {script} killed after 110s")
        except Exception as e:  # noqa: BLE001
            self._log(f"crond: {script} failed to start: {e}")
        finally:
            with self._lock:
                self._running.discard(key)
        _ = started

    def _validate(self, command: str) -> list[str] | None:
        """Accept `cd <repo> && <python> scripts/x.py [args] [>> logs/cron.log 2>&1]`; return ['python', 'scripts/x.py', *args]."""
        cmd = command.strip()
        m = re.match(r"^cd\s+(\S+)\s*&&\s*(\S+)\s+(scripts/[\w.-]+\.py)(.*)$", cmd)
        if not m:
            return None
        cd_target, prog, script, rest = m.groups()
        if Path(cd_target).resolve() != self.world.repo.resolve():
            return None
        if Path(prog).name not in ("python", "python3") and prog != self.world.python:
            return None
        rest = re.sub(r">>\s*logs/cron\.log\s*2>&1\s*$", "", rest).strip()
        try:
            args = shlex.split(rest)
        except ValueError:
            return None
        if any(a for a in args if a.startswith(("/", "~")) or ".." in a or any(ch in a for ch in ";&|<>`$")):
            return None
        return ["python", script, *args]
