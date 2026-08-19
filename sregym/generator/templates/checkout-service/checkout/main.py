"""checkout-service HTTP API.

Endpoints:
  GET  /health            liveness + database connectivity
  GET  /users, /users/{id}
  GET  /orders, /orders/{id}
#[[ checkout
  POST /checkout          create an order and capture payment
#]] checkout
#[[ metrics
  GET  /metrics           Prometheus text exposition
#]] metrics
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__, telemetry
from .config import settings
from .db import core_db, ping
#[[ ledger
from .db import ledger_db
#]] ledger

log = logging.getLogger("checkout.app")
access_log = logging.getLogger("checkout.access")
ratelimit_log = logging.getLogger("checkout.ratelimit")

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_PKG_DIR)


def _git_commit() -> str:
    """Best-effort short SHA of the deployed revision (read from .git, no subprocess)."""
    try:
        head = open(os.path.join(_REPO_DIR, ".git", "HEAD")).read().strip()
        if head.startswith("ref: "):
            ref_path = os.path.join(_REPO_DIR, ".git", head[5:])
            if os.path.exists(ref_path):
                return open(ref_path).read().strip()[:7]
            packed = os.path.join(_REPO_DIR, ".git", "packed-refs")
            if os.path.exists(packed):
                for line in open(packed):
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == head[5:]:
                        return parts[0][:7]
            return "unknown"
        return head[:7]
    except OSError:
        return "unknown"


COMMIT = _git_commit()

app = FastAPI(title=settings.app_name, version=__version__, docs_url=None, redoc_url=None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(path: str) -> str:
    try:
        return os.path.relpath(path, _REPO_DIR)
    except ValueError:
        return path


def _log_exception(req_id: str, exc: BaseException) -> None:
    """Log the traceback restricted to our own frames, one log line per traceback line."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename.startswith(_REPO_DIR)]
    lines = ["Traceback (most recent call last):"]
    for f in frames:
        lines.append(f'  File "{_rel(f.filename)}", line {f.lineno}, in {f.name}')
        if f.line:
            lines.append(f"    {f.line.strip()}")
    lines.append(_describe_exception(exc))
    for line in lines:
        log.error("req=%s %s", req_id, line)


def _describe_exception(exc: BaseException) -> str:
    cls = type(exc)
    name = cls.__name__ if cls.__module__ in ("builtins", "__main__") else f"{cls.__module__}.{cls.__name__}"
    return f"{name}: {exc}"


@app.middleware("http")
async def observe_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
    req_id = uuid.uuid4().hex[:8]
    request.state.request_id = req_id
    request.state.log_extra = {}
    started = time.perf_counter()
    error: str | None = None
    try:
        response = await call_next(request)
    except Exception as exc:  # unhandled -> 500
        error = _describe_exception(exc)
        _log_exception(req_id, exc)
        response = JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Something went wrong, please try again.", "request_id": req_id},
        )
    duration_ms = (time.perf_counter() - started) * 1000
    route = request.scope.get("route")
    path_template = getattr(route, "path", request.url.path)
    telemetry.observe_request(request.method, path_template, response.status_code, duration_ms)
    extra = " ".join(f"{k}={v}" for k, v in request.state.log_extra.items())
    msg = f"req={req_id} {request.method} {request.url.path} {response.status_code} {duration_ms:.0f}ms"
    if extra:
        msg += " " + extra
    if error:
        msg += f' error="{error}"'
        access_log.error(msg)
    elif response.status_code >= 500:
        access_log.error(msg)
    else:
        access_log.info(msg)
    response.headers["x-request-id"] = req_id
    return response


# --------------------------------------------------------------------------- health
@app.get("/health")
def health() -> JSONResponse:
    checks: dict[str, str] = {}
    targets = {"core_db": settings.database_url}
    #[[ ledger
    targets["ledger_db"] = settings.ledger_database_url
    #]] ledger
    for name, url in targets.items():
        try:
            ping(url)
            checks[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - report anything
            checks[name] = f"error: {exc}"
            telemetry.db_error(name.replace("_db", ""))
    healthy = all(v == "ok" for v in checks.values())
    body = {
        "status": "ok" if healthy else "degraded",
        "service": settings.app_name,
        "version": __version__,
        "commit": COMMIT,
        "checks": checks,
        "time": _now_iso(),
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)


# --------------------------------------------------------------------------- users
def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


@app.get("/users")
def list_users(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    with core_db() as conn:
        rows = conn.execute(
            "SELECT id, email, full_name, country, tier, created_at FROM users ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {"users": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@app.get("/users/{user_id}")
def get_user(user_id: int, request: Request) -> dict[str, Any]:
    request.state.log_extra["user"] = user_id
    with core_db() as conn:
        user = _row(conn.execute("SELECT id, email, full_name, country, tier, created_at FROM users WHERE id = ?", (user_id,)).fetchone())
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        stats = conn.execute(
            "SELECT COUNT(*) AS order_count, COALESCE(SUM(total_cents), 0) AS lifetime_cents FROM orders WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    user["order_count"] = stats["order_count"]
    user["lifetime_value_cents"] = stats["lifetime_cents"]
    return user


# --------------------------------------------------------------------------- orders
@app.get("/orders")
def list_orders(
    request: Request,
    user_id: int | None = None,
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    if user_id is not None:
        request.state.log_extra["user"] = user_id
    clauses, params = [], []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with core_db() as conn:
        rows = conn.execute(
            f"SELECT id, user_id, status, total_cents, currency, created_at, updated_at FROM orders {where} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM orders {where}", params).fetchone()[0]
    return {"orders": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@app.get("/orders/{order_id}")
def get_order(order_id: int, request: Request) -> dict[str, Any]:
    request.state.log_extra["order"] = order_id
    with core_db() as conn:
        order = _row(conn.execute(
            "SELECT id, user_id, status, total_cents, currency, created_at, updated_at FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone())
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        items = conn.execute(
            "SELECT oi.product_id, p.sku, p.name, oi.quantity, oi.unit_price_cents "
            "FROM order_items oi JOIN products p ON p.id = oi.product_id WHERE oi.order_id = ? ORDER BY oi.id",
            (order_id,),
        ).fetchall()
    order["items"] = [dict(r) for r in items]
    return order


#[[ checkout
# --------------------------------------------------------------------------- checkout
class CheckoutItem(BaseModel):
    sku: str
    quantity: int = Field(1, ge=1, le=50)


class CheckoutRequest(BaseModel):
    user_id: int
    items: list[CheckoutItem] = Field(..., min_length=1, max_length=25)
    payment_method: str = Field("card", pattern="^(card|paypal|apple_pay|bank_transfer)$")
    currency: str = Field("USD", min_length=3, max_length=3)


class _RateLimiter:
    """Fixed-window per-user limiter for POST /checkout (in-memory, per process)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._lock = threading.Lock()
        self._counts: dict[tuple[int, int], int] = {}

    def allow(self, user_id: int) -> bool:
        window = int(time.time() // 60)
        with self._lock:
            # drop old windows
            for key in [k for k in self._counts if k[1] < window]:
                del self._counts[key]
            n = self._counts.get((user_id, window), 0) + 1
            self._counts[(user_id, window)] = n
            return n <= self.per_minute


_limiter = _RateLimiter(settings.rate_limit_per_minute)


def _authorize_payment(amount_cents: int, method: str, currency: str) -> str:
    """Talk to the payment gateway. In ``stub`` mode (all non-live environments) we
    authorize locally and mint a synthetic reference; ``live`` mode is not implemented
    in this build."""
    if settings.payment_gateway_mode != "stub":
        raise RuntimeError(f"payment gateway mode {settings.payment_gateway_mode!r} not supported")
    return "ch_" + uuid.uuid4().hex[:16]


@app.post("/checkout", status_code=201)
def checkout(payload: CheckoutRequest, request: Request) -> dict[str, Any]:
    request.state.log_extra["user"] = payload.user_id
    if not _limiter.allow(payload.user_id):
        telemetry.rate_limited()
        ratelimit_log.warning("user=%s exceeded %d checkouts/min", payload.user_id, settings.rate_limit_per_minute)
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    created_at = _now_iso()
    with core_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE id = ?", (payload.user_id,)).fetchone()
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        skus = [i.sku for i in payload.items]
        placeholders = ",".join("?" for _ in skus)
        products = {
            r["sku"]: r
            for r in conn.execute(f"SELECT id, sku, price_cents, active FROM products WHERE sku IN ({placeholders})", skus)
        }
        unknown = [s for s in skus if s not in products or not products[s]["active"]]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown or inactive sku(s): {', '.join(unknown)}")
        total = sum(products[i.sku]["price_cents"] * i.quantity for i in payload.items)
        cur = conn.execute(
            "INSERT INTO orders (user_id, status, total_cents, currency, created_at, updated_at) VALUES (?, 'pending', ?, ?, ?, ?)",
            (payload.user_id, total, payload.currency.upper(), created_at, created_at),
        )
        order_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents) VALUES (?, ?, ?, ?)",
            [(order_id, products[i.sku]["id"], i.quantity, products[i.sku]["price_cents"]) for i in payload.items],
        )
        gateway_ref = _authorize_payment(total, payload.payment_method, payload.currency)
        #[[ ledger
        # Record the captured payment in the ledger before confirming the order. If the
        # ledger write fails the surrounding core transaction rolls back too.
        with ledger_db() as ledger:
            pcur = ledger.execute(
                "INSERT INTO payments (order_id, user_id, amount_cents, currency, method, status, gateway_ref, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'captured', ?, ?)",
                (order_id, payload.user_id, total, payload.currency.upper(), payload.payment_method, gateway_ref, created_at),
            )
            payment_id = pcur.lastrowid
        #]] ledger
        conn.execute("UPDATE orders SET status = 'confirmed', updated_at = ? WHERE id = ?", (created_at, order_id))
    request.state.log_extra["order"] = order_id
    return {
        "order_id": order_id,
        #[[ ledger
        "payment_id": payment_id,
        #]] ledger
        "status": "confirmed",
        "total_cents": total,
        "currency": payload.currency.upper(),
        "gateway_ref": gateway_ref,
        "created_at": created_at,
    }


#]] checkout
#[[ metrics
# --------------------------------------------------------------------------- metrics
@app.get("/metrics")
def metrics() -> PlainTextResponse:
    return PlainTextResponse(telemetry.render(__version__, COMMIT), media_type="text/plain; version=0.0.4")


#]] metrics
@app.get("/")
def root() -> dict[str, str]:
    return {"service": settings.app_name, "version": __version__, "commit": COMMIT}
