-- 0001: the migration ledger.
-- Shipped migrations are never edited; add 0002_*.sql for the next change.

CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
