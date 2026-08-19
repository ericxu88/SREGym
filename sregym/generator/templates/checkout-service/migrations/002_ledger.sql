-- 002_ledger: payments ledger (separate database file, see LEDGER_DATABASE_URL)
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'USD',
    method       TEXT NOT NULL,
    status       TEXT NOT NULL,
    gateway_ref  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at);

INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES ('002_ledger', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
