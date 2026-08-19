# checkout-service

Order checkout API for __SREGYM_COMPANY__ (`__SREGYM_DOMAIN__`). Small FastAPI service
backed by SQLite; fronted by nginx in production.

## Endpoints

| Method | Path            | Notes                                              |
|--------|-----------------|----------------------------------------------------|
| GET    | `/health`       | liveness + database connectivity (503 if degraded) |
| GET    | `/users`        | paginated users (`limit`, `offset`)                |
| GET    | `/users/{id}`   | user + order stats                                 |
| GET    | `/orders`       | filter by `user_id`, `status`                      |
| GET    | `/orders/{id}`  | order with line items                              |
#[[ checkout
| POST   | `/checkout`     | create order, capture payment, returns 201         |
#]] checkout
#[[ metrics
| GET    | `/metrics`      | Prometheus text format (scraped every 15s)         |
#]] metrics

## Configuration

All configuration comes from `.env` in the working directory (see `checkout/config.py`
for keys and defaults). Real environment variables override the file. **The
production `.env` is tracked in this repo and shipped by deploy-bot on merge to
`main`; the service is restarted as part of every deploy.**

Key settings:

- `DATABASE_URL` – sqlite URL of the core database (users, products, orders, carts)
#[[ ledger
- `LEDGER_DATABASE_URL` – sqlite URL of the payments ledger (separate file for audit isolation)
#]] ledger
- `LOG_PATH` / `LOG_LEVEL` – application log destination
- `RATE_LIMIT_PER_MINUTE` – per-user checkout rate limit
#[[ checkout
- `PAYMENT_GATEWAY_*` – gateway endpoint/timeout; `PAYMENT_GATEWAY_MODE=stub` authorizes locally
- `CART_TTL_MINUTES` – abandoned-cart expiry used by `scripts/expire_carts.py`
#]] checkout

## Running

```
python -m checkout.serve          # reads ./.env, logs to LOG_PATH
```

Schema lives in `migrations/*.sql` and is applied by the provisioning playbook.

#[[ cron
## Scheduled jobs

`scripts/expire_carts.py` runs from cron every 15 minutes and writes to `logs/cron.log`.

#]] cron
#[[ runbook
## On-call notes

- Health: `GET /health` reports per-database connectivity.
- Logs: `logs/app.log` (access + application, UTC). Deploys: `logs/deploy.log`.
- Metrics: `http_requests_total`, `db_errors_total`, `http_request_duration_ms_*`.
- Restart: `systemctl restart checkout-service` (or the service manager on the host).
#]] runbook
