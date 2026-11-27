-- Status stays plain TEXT so later statuses do not require rebuilding this table.
CREATE TABLE items (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL CHECK (length(trim(title)) > 0),
    notes        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    completed_at TEXT
);
