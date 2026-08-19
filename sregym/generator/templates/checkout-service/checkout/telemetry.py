"""In-process metrics registry, exported in Prometheus text format by GET /metrics."""
from __future__ import annotations

import threading
import time
from collections import defaultdict

_lock = threading.Lock()
_started = time.time()

_requests: dict[tuple[str, str, str], int] = defaultdict(int)  # (method, path, status) -> count
_duration_sum: dict[str, float] = defaultdict(float)  # path -> ms
_duration_count: dict[str, int] = defaultdict(int)
_db_errors: dict[str, int] = defaultdict(int)  # db -> count
_ratelimited: int = 0


def observe_request(method: str, path: str, status: int, duration_ms: float) -> None:
    with _lock:
        _requests[(method, path, str(status))] += 1
        _duration_sum[path] += duration_ms
        _duration_count[path] += 1


def db_error(db: str) -> None:
    with _lock:
        _db_errors[db] += 1


def rate_limited() -> None:
    global _ratelimited
    with _lock:
        _ratelimited += 1


def _labels(**kw: str) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in kw.items()) + "}"


def render(version: str, commit: str) -> str:
    lines: list[str] = []
    with _lock:
        lines.append("# HELP http_requests_total Total HTTP requests by method, path template and status.")
        lines.append("# TYPE http_requests_total counter")
        for (method, path, status), n in sorted(_requests.items()):
            lines.append(f"http_requests_total{_labels(method=method, path=path, status=status)} {n}")
        lines.append("# HELP http_request_duration_ms_sum Sum of request latencies in milliseconds.")
        lines.append("# TYPE http_request_duration_ms_sum counter")
        for path, s in sorted(_duration_sum.items()):
            lines.append(f"http_request_duration_ms_sum{_labels(path=path)} {s:.3f}")
        lines.append("# HELP http_request_duration_ms_count Number of latency observations.")
        lines.append("# TYPE http_request_duration_ms_count counter")
        for path, c in sorted(_duration_count.items()):
            lines.append(f"http_request_duration_ms_count{_labels(path=path)} {c}")
        lines.append("# HELP db_errors_total Database connection/query errors by database.")
        lines.append("# TYPE db_errors_total counter")
        for db, n in sorted(_db_errors.items()):
            lines.append(f"db_errors_total{_labels(db=db)} {n}")
        lines.append("# HELP rate_limited_requests_total Requests rejected by the per-user rate limiter.")
        lines.append("# TYPE rate_limited_requests_total counter")
        lines.append(f"rate_limited_requests_total {_ratelimited}")
        lines.append("# HELP process_start_time_seconds Unix time the process started.")
        lines.append("# TYPE process_start_time_seconds gauge")
        lines.append(f"process_start_time_seconds {_started:.0f}")
        lines.append("# HELP app_info Build information.")
        lines.append("# TYPE app_info gauge")
        lines.append(f"app_info{_labels(version=version, commit=commit)} 1")
    return "\n".join(lines) + "\n"
