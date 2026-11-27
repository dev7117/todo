CREATE TABLE projects (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused', 'done', 'archived')),
    target_on   TEXT,
    legacy_id   INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

ALTER TABLE tasks ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;
CREATE INDEX tasks_project_id ON tasks(project_id);

-- Extend the CHECK without editing 0003; copy every history row verbatim.
CREATE TABLE task_history_projects (
    id        INTEGER PRIMARY KEY,
    task_id   INTEGER NOT NULL REFERENCES tasks(id),
    timestamp TEXT NOT NULL,
    field     TEXT NOT NULL CHECK (field IN ('create', 'title', 'notes', 'status', 'parent_id', 'project_id')),
    old_value TEXT,
    new_value TEXT
);
INSERT INTO task_history_projects SELECT * FROM task_history;
DROP TABLE task_history;
ALTER TABLE task_history_projects RENAME TO task_history;
CREATE INDEX task_history_task_id ON task_history(task_id, id);
CREATE TRIGGER task_history_no_update BEFORE UPDATE ON task_history
BEGIN
    SELECT RAISE(ABORT, 'Task history is append-only');
END;
CREATE TRIGGER task_history_no_delete BEFORE DELETE ON task_history
BEGIN
    SELECT RAISE(ABORT, 'Task history is append-only');
END;
