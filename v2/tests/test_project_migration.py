"""The invented schema-3 fixture is always copied; migration preserves its audit trail."""
from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import sqlite3

import pytest

from todo_v2 import migrate
from todo_v2.db import connect

FIXTURE = Path(__file__).parent / "fixtures" / "tasks-v3.sqlite3"


def test_tasks_fixture_migration_preserves_rows_history_and_source(tmp_path):
    checksum = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    db = tmp_path / "projects.db"
    shutil.copyfile(FIXTURE, db)
    with closing(connect(db)) as conn:
        assert migrate.schema_version(conn) == 3
        before = [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
        history = [dict(row) for row in conn.execute("SELECT * FROM task_history ORDER BY id")]
        assert len(before) >= 3 and len(history) >= 3
    through_four = tmp_path / "through-four"
    through_four.mkdir()
    for migration in migrate.discover()[:4]:
        shutil.copyfile(migration.path, through_four / migration.path.name)
    result = migrate.migrate(db, through_four)
    assert result.applied == [4]
    assert result.schema_version == 4
    assert result.backup is not None
    with closing(connect(db)) as conn, conn:
        assert migrate.schema_version(conn) == conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")] == [
            {**row, "project_id": None} for row in before]
        assert [dict(row) for row in conn.execute("SELECT * FROM task_history ORDER BY id")] == history
        assert conn.execute("SELECT * FROM projects").fetchall() == []
        conn.execute("INSERT INTO projects (title, created_at, updated_at) VALUES ('Kitchen refresh', 'unchanged', 'unchanged')")
        for sql in ("UPDATE projects SET title = '  '", "UPDATE projects SET status = 'cancelled'",
                    "UPDATE tasks SET project_id = 999", "INSERT INTO task_history (task_id, timestamp, field) VALUES (1, 'unchanged', 'unknown')"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        conn.execute("UPDATE tasks SET project_id = 1 WHERE id = 1")
        conn.execute("INSERT INTO task_history (task_id, timestamp, field, new_value) VALUES (1, 'unchanged', 'project_id', '1')")
        for sql in ("UPDATE task_history SET new_value = 'erased'", "DELETE FROM task_history"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
        conn.execute("DELETE FROM projects WHERE id = 1")
        assert conn.execute("SELECT project_id FROM tasks WHERE id = 1").fetchone()[0] is None
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with closing(connect(result.backup)) as conn:
        assert migrate.schema_version(conn) == 3
        assert [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")] == before
        assert [dict(row) for row in conn.execute("SELECT * FROM task_history ORDER BY id")] == history
    latest = migrate.migrate(db)
    assert latest.schema_version == migrate.discover()[-1].version
    with closing(connect(db)) as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM task_history ORDER BY id")][:len(history)] == history
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == checksum
