CREATE TABLE ideas (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'seed'
        CHECK (status IN ('seed', 'exploring', 'parked', 'promoted', 'dropped')),
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    legacy_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ideas_project_id ON ideas(project_id);

CREATE TABLE blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    idea_id INTEGER REFERENCES ideas(id),
    kind TEXT NOT NULL CHECK (kind IN ('note', 'subtask')),
    body TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL CHECK (position >= 0),
    done INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
    done_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((task_id IS NOT NULL) != (idea_id IS NOT NULL)),
    CHECK (kind = 'subtask' OR (done = 0 AND done_at IS NULL)),
    CHECK ((done = 0 AND done_at IS NULL) OR (done = 1 AND done_at IS NOT NULL))
);
CREATE UNIQUE INDEX blocks_task_position ON blocks(task_id, position) WHERE task_id IS NOT NULL;
CREATE UNIQUE INDEX blocks_idea_position ON blocks(idea_id, position) WHERE idea_id IS NOT NULL;

-- Keep every audit row and both append-only triggers while extending the CHECK.
CREATE TABLE task_history_blocks (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    timestamp TEXT NOT NULL,
    field TEXT NOT NULL CHECK (field IN ('create', 'title', 'notes', 'status', 'parent_id', 'project_id', 'blocks')),
    old_value TEXT,
    new_value TEXT
);
INSERT INTO task_history_blocks SELECT * FROM task_history;
DROP TABLE task_history;
ALTER TABLE task_history_blocks RENAME TO task_history;
CREATE INDEX task_history_task_id ON task_history(task_id, id);
CREATE TRIGGER task_history_no_update BEFORE UPDATE ON task_history
BEGIN
    SELECT RAISE(ABORT, 'Task history is append-only');
END;
CREATE TRIGGER task_history_no_delete BEFORE DELETE ON task_history
BEGIN
    SELECT RAISE(ABORT, 'Task history is append-only');
END;
