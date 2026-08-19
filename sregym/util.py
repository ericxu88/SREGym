"""Small shared helpers: env-file parsing, hashing, time formatting, sqlite URLs.

The env-file parser here MUST stay semantically identical to the copy embedded in
the generated application (``checkout/config.py`` template): the verifier uses this
one to decide whether the agent's fix is what the app would actually load.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# --------------------------------------------------------------------------- env files
def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Later keys override earlier ones. Quotes are stripped.

    Supports ``export KEY=VALUE``, blank lines and ``#`` comments. Inline comments are
    NOT stripped from unquoted values (matches the app's loader).
    """
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


def parse_sqlite_url(url: str) -> str:
    """Return the filesystem path encoded in a ``sqlite:///path`` URL.

    ``sqlite:///data/x.db`` -> ``data/x.db`` (relative), ``sqlite:////abs/x.db`` -> ``/abs/x.db``.
    Raises ValueError for anything that is not a sqlite URL.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ValueError(f"unsupported database URL (expected sqlite:///...): {url!r}")
    path = url[len(prefix):]
    # strip a query string like ?mode=rw
    path = path.split("?", 1)[0]
    if not path:
        raise ValueError(f"empty sqlite path in URL {url!r}")
    return path


# --------------------------------------------------------------------------- hashing
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(obj: Any) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, default=str))


def head_hash(path: Path, nbytes: int = 4096) -> str:
    with open(path, "rb") as f:
        return sha256_bytes(f.read(nbytes))


# --------------------------------------------------------------------------- time
LOG_TS_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def fmt_log_ts(dt: datetime) -> str:
    """``2026-08-18 14:22:31.412`` -- the format used by the app's log formatter."""
    return dt.strftime(LOG_TS_FMT) + f".{dt.microsecond // 1000:03d}"


def fmt_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_hm(dt: datetime) -> str:
    return dt.strftime("%H:%M UTC")


def parse_iso(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- misc
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def count_lines(path: Path) -> int:
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
    return n


def relpath(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def truncate_text(text: str, limit: int, marker: str = "\n... [truncated {n} chars] ...\n") -> str:
    if len(text) <= limit:
        return text
    keep = limit // 2
    return text[:keep] + marker.format(n=len(text) - 2 * keep) + text[-keep:]


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def iter_files(root: Path, exclude_dirs: Iterable[str] = ()) -> Iterable[Path]:
    """Yield all files under root, skipping any directory whose relative first component
    (or name) is in exclude_dirs."""
    excl = set(exclude_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        parts = rel.parts
        dirnames[:] = sorted(d for d in dirnames if d not in excl and not (parts == () and d in excl))
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


# --------------------------------------------------------------------------- http
def http_request(method: str, url: str, body: Any = None, timeout: float = 5.0,
                 headers: dict[str, str] | None = None) -> tuple[int, str]:
    """Minimal HTTP client returning (status, text). Network errors -> status 0."""
    import urllib.error
    import urllib.request

    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        return e.code, text
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        return 0, f"connection error: {getattr(e, 'reason', e)}"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
