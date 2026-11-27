-- Rebuild once to enforce the task status contract; preserve the items-era data.
CREATE TABLE tasks (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL CHECK (length(trim(title)) > 0),
    notes        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'todo'
                 CHECK (status IN ('inbox', 'todo', 'in_progress', 'waiting', 'done', 'cancelled')),
    parent_id    INTEGER REFERENCES tasks(id) CHECK (parent_id != id),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    completed_at TEXT
);

INSERT INTO tasks (id, title, notes, status, created_at, updated_at, completed_at)
SELECT id, title, notes, CASE status WHEN 'open' THEN 'todo' ELSE status END,
       created_at, updated_at, completed_at
FROM items;
DROP TABLE items;

CREATE INDEX tasks_parent_id ON tasks(parent_id);

-- History begins with new API writes; the items era did not record change history.
CREATE TABLE task_history (
    id        INTEGER PRIMARY KEY,
    task_id   INTEGER NOT NULL REFERENCES tasks(id),
    timestamp TEXT NOT NULL,
    field     TEXT NOT NULL CHECK (field IN ('create', 'title', 'notes', 'status', 'parent_id')),
    old_value TEXT,
    new_value TEXT
);
CREATE INDEX task_history_task_id ON task_history(task_id, id);

CREATE TRIGGER task_history_no_update BEFORE UPDATE ON task_history
BEGIN
    SELECT RAISE(ABORT, 'Task history is append-only');
END;
CREATE TRIGGER task_history_no_delete BEFORE DELETE ON task_history
BEGIN
    SELECT RAISE(ABORT, 'Task history is append-only');
END;
