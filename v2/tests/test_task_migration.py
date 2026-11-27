"""The committed items-era fixture is invented and must never be migrated in place."""
from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import sqlite3

import pytest

from todo_v2 import migrate
from todo_v2.db import BUSY_TIMEOUT_MS, connect

FIXTURE = Path(__file__).parent / "fixtures" / "items-v2.sqlite3"


def test_items_fixture_migration_preserves_rows_and_source(tmp_path):
    checksum = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    db = tmp_path / "tasks.db"
    shutil.copyfile(FIXTURE, db)
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        assert migrate.schema_version(conn) == 2
        before = [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY id")]
        assert {row["status"] for row in before} == {"open", "done"}
    result = migrate.migrate(db)
    assert result.applied == [3]
    assert result.schema_version == 3
    assert result.backup is not None
    with closing(connect(db)) as conn:
        assert migrate.schema_version(conn) == conn.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"tasks", "task_history"} <= tables
        assert "items" not in tables
        after = [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
        assert after == [{**row, "status": "todo" if row["status"] == "open" else "done", "parent_id": None} for row in before]
        assert conn.execute("SELECT count(*) FROM task_history").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with closing(sqlite3.connect(result.backup)) as conn:
        assert migrate.schema_version(conn) == 2
        conn.row_factory = sqlite3.Row
        assert [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY id")] == before
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == checksum


@pytest.mark.parametrize("read_only", [False, True])
def test_v2_connection_policy(tmp_path, read_only):
    db = tmp_path / "tasks.db"
    migrate.migrate(db)
    with closing(connect(db, read_only=read_only)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS == 5000


def test_sql_constraints_and_append_only_history(tmp_path):
    db = tmp_path / "tasks.db"
    shutil.copyfile(FIXTURE, db)
    migrate.migrate(db)
    with closing(connect(db)) as conn, conn:
        task_id = conn.execute("SELECT id FROM tasks LIMIT 1").fetchone()[0]
        for sql, params in [
            ("UPDATE tasks SET parent_id = id WHERE id = ?", (task_id,)),
            ("UPDATE tasks SET parent_id = 999 WHERE id = ?", (task_id,)),
            ("UPDATE tasks SET status = 'open' WHERE id = ?", (task_id,)),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, params)
        conn.execute("INSERT INTO task_history (task_id, timestamp, field, new_value) VALUES (?, ?, 'notes', 'Filed')",
                     (task_id, "2026-11-01T12:00:00.123456Z"))
        for sql in ("UPDATE task_history SET new_value = 'Erased'", "DELETE FROM task_history"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
        assert conn.execute("SELECT new_value FROM task_history").fetchone()[0] == "Filed"
