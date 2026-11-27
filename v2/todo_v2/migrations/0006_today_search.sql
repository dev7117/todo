-- Read-time Today membership; past dates remain available for future carry-over.
ALTER TABLE tasks ADD COLUMN today_on TEXT;
CREATE INDEX tasks_today_on ON tasks(today_on);

CREATE TABLE task_history_today (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    timestamp TEXT NOT NULL,
    field TEXT NOT NULL CHECK (field IN ('create', 'title', 'notes', 'status', 'parent_id', 'project_id', 'blocks', 'today_on')),
    old_value TEXT,
    new_value TEXT
);
INSERT INTO task_history_today SELECT * FROM task_history;
DROP TABLE task_history;
ALTER TABLE task_history_today RENAME TO task_history;
CREATE INDEX task_history_task_id ON task_history(task_id, id);
CREATE TRIGGER task_history_no_update BEFORE UPDATE ON task_history
BEGIN SELECT RAISE(ABORT, 'Task history is append-only'); END;
CREATE TRIGGER task_history_no_delete BEFORE DELETE ON task_history
BEGIN SELECT RAISE(ABORT, 'Task history is append-only'); END;

-- External content keeps the source table authoritative. Triggers cover every write path.
CREATE VIRTUAL TABLE tasks_fts USING fts5(title, notes, content='tasks', content_rowid='id');
CREATE TRIGGER tasks_fts_insert AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, title, notes) VALUES (new.id, new.title, new.notes);
END;
CREATE TRIGGER tasks_fts_delete AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, notes) VALUES ('delete', old.id, old.title, old.notes);
END;
CREATE TRIGGER tasks_fts_update AFTER UPDATE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, notes) VALUES ('delete', old.id, old.title, old.notes);
    INSERT INTO tasks_fts(rowid, title, notes) VALUES (new.id, new.title, new.notes);
END;
INSERT INTO tasks_fts(tasks_fts) VALUES ('rebuild');

-- External content keeps the source table authoritative. Triggers cover every write path.
CREATE VIRTUAL TABLE ideas_fts USING fts5(title, summary, content='ideas', content_rowid='id');
CREATE TRIGGER ideas_fts_insert AFTER INSERT ON ideas BEGIN
    INSERT INTO ideas_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TRIGGER ideas_fts_delete AFTER DELETE ON ideas BEGIN
    INSERT INTO ideas_fts(ideas_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
END;
CREATE TRIGGER ideas_fts_update AFTER UPDATE ON ideas BEGIN
    INSERT INTO ideas_fts(ideas_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
    INSERT INTO ideas_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
INSERT INTO ideas_fts(ideas_fts) VALUES ('rebuild');

-- External content keeps the source table authoritative. Triggers cover every write path.
CREATE VIRTUAL TABLE blocks_fts USING fts5(body, content='blocks', content_rowid='id');
CREATE TRIGGER blocks_fts_insert AFTER INSERT ON blocks BEGIN
    INSERT INTO blocks_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER blocks_fts_delete AFTER DELETE ON blocks BEGIN
    INSERT INTO blocks_fts(blocks_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TRIGGER blocks_fts_update AFTER UPDATE ON blocks BEGIN
    INSERT INTO blocks_fts(blocks_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO blocks_fts(rowid, body) VALUES (new.id, new.body);
END;
INSERT INTO blocks_fts(blocks_fts) VALUES ('rebuild');
