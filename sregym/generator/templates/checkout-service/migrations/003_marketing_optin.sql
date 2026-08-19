-- 003_marketing_optin: marketing consent flag on users (GROWTH-142)
ALTER TABLE users ADD COLUMN marketing_opt_in INTEGER NOT NULL DEFAULT 0;
INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES ('003_marketing_optin', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
