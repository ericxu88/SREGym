-- 003_fulfillment: fulfillment status on orders (OPS-610)
ALTER TABLE orders ADD COLUMN fulfillment_status TEXT NOT NULL DEFAULT 'unfulfilled';
CREATE INDEX IF NOT EXISTS idx_orders_fulfillment ON orders(fulfillment_status);
INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES ('003_fulfillment', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
