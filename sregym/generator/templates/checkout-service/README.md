# __SREGYM_SERVICE__

Order checkout API for __SREGYM_COMPANY__ (`__SREGYM_DOMAIN__`). Small FastAPI service
backed by SQLite; fronted by nginx in production.

## Endpoints

| Method | Path            | Notes                                              |
|--------|-----------------|----------------------------------------------------|
| GET    | `/health`       | liveness + database connectivity (503 if degraded) |
| GET    | `__SREGYM_ROUTE_PREFIX__/users`        | paginated users (`limit`, `offset`)                |
| GET    | `__SREGYM_ROUTE_PREFIX__/users/{id}`   | user + order stats                                 |
| GET    | `__SREGYM_ROUTE_PREFIX__/orders`       | filter by `user_id`, `status`                      |
| GET    | `__SREGYM_ROUTE_PREFIX__/orders/{id}`  | order with line items                              |
#[[ checkout
| POST   | `__SREGYM_CHECKOUT_ROUTE__`     | create order, capture payment, returns 201         |
#]] checkout
#[[ metrics
| GET    | `/metrics`      | Prometheus text format (scraped every 15s)         |
#]] metrics

## Configuration

All configuration comes from `.env` in the working directory (see `__SREGYM_PKG__/config.py`
for keys and defaults). Real environment variables override the file. **The
production `.env` is tracked in this repo and shipped by deploy-bot on merge to
`main`; the service is restarted as part of every deploy.**

Key settings:

- `DATABASE_URL` – sqlite URL of the core database (users, products, orders, carts)
#[[ ledger
- `LEDGER_DATABASE_URL` – sqlite URL of the payments ledger (separate file for audit isolation).
  A weekly audit snapshot of the ledger is written to `data/ledger-snapshot-YYYYMMDD.db` by cron;
  `scripts/reconcile_ledger.py` compares confirmed orders with ledger payments (and can copy
  missing payments from another ledger-format file with `--source ... --apply`).
#]] ledger
- `LOG_PATH` / `LOG_LEVEL` – application log destination
- `RATE_LIMIT_PER_MINUTE` – per-user checkout rate limit
#[[ checkout
- `PAYMENT_GATEWAY_*` – gateway endpoint/timeout; `PAYMENT_GATEWAY_MODE=stub` authorizes locally
- `CART_TTL_MINUTES` – abandoned-cart expiry used by `scripts/expire_carts.py`
#]] checkout

## Running

```
python -m __SREGYM_PKG__.serve          # reads ./.env, logs to LOG_PATH
```

Schema lives in `migrations/*.sql`. deploy-bot does **not** run migrations; apply pending ones
with `python scripts/migrate.py --apply` (plain `python scripts/migrate.py` shows status).

#[[ cron
## Scheduled jobs

`scripts/expire_carts.py` runs from cron every 15 minutes and writes to `logs/cron.log`.

#]] cron
#[[ runbook
## On-call notes

- Health: `GET /health` reports per-database connectivity.
- Logs: `logs/app.log` (access + application, UTC). Deploys: `logs/deploy.log`.
- Metrics: `http_requests_total`, `db_errors_total`, `http_request_duration_ms_*`; the ledger exporter
  publishes `ledger_payments_total` and `ledger_last_payment_age_seconds` (finance alerts on freshness).
- Restart: `systemctl restart __SREGYM_SERVICE__` (or the service manager on the host).
#]] runbook
