-- 0001: the migration ledger and a small key/value settings table.
-- Shipped migrations are never edited; add 0002_*.sql for the next change.

CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
