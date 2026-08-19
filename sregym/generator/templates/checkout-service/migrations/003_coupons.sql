-- 003_coupons: coupon codes at checkout (CHK-301)
ALTER TABLE orders ADD COLUMN coupon_code TEXT;
ALTER TABLE orders ADD COLUMN discount_cents INTEGER NOT NULL DEFAULT 0;
INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES ('003_coupons', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
