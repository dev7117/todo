"""Invented schema-4 data upgrades using one online backup; source stays untouched."""
from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import sqlite3

import pytest

from todo_v2 import migrate
from todo_v2.db import connect

FIXTURE = Path(__file__).parent / "fixtures" / "projects-v4.sqlite3"


def snapshot(conn):
    return {table: [{key: row[key] for key in row.keys() if key != "today_on"} for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("projects", "tasks", "task_history")}


def test_schema_four_fixture_backup_and_preservation(tmp_path, monkeypatch):
    checksum = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    db = tmp_path / "ideas.db"
    shutil.copyfile(FIXTURE, db)
    with closing(connect(db)) as conn:
        assert migrate.schema_version(conn) == 4
        before = snapshot(conn)
        assert all(len(rows) >= 2 for rows in before.values())
    original_backup = migrate.backup
    calls = []
    def backup(conn, path, current):
        assert current == 4 and migrate.schema_version(conn) == 4
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'ideas'").fetchone() is None
        calls.append(current)
        return original_backup(conn, path, current)
    monkeypatch.setattr(migrate, "backup", backup)
    original_apply = migrate._apply
    def apply(conn, migration):
        original_apply(conn, migration)
        if migration.version == 5:
            # Pin the 0005 rewrite before subsequent migrations run.
            assert snapshot(conn) == before
            assert conn.execute("SELECT * FROM ideas").fetchall() == []
            assert conn.execute("SELECT * FROM blocks").fetchall() == []
            assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")} >= {
                "task_history_no_update", "task_history_no_delete"}
    monkeypatch.setattr(migrate, "_apply", apply)
    result = migrate.migrate(db)
    assert result.applied == [m.version for m in migrate.discover() if m.version > 4]
    assert result.schema_version == migrate.discover()[-1].version
    assert calls == [4] and result.backup is not None
    assert len(list(tmp_path.glob("*.bak"))) == 1
    with closing(connect(result.backup)) as conn:
        assert migrate.schema_version(conn) == 4 and snapshot(conn) == before
    with closing(connect(db)) as conn, conn:
        assert snapshot(conn) == before
        assert migrate.schema_version(conn) == conn.execute("PRAGMA user_version").fetchone()[0] == migrate.discover()[-1].version
        assert conn.execute("SELECT * FROM ideas").fetchall() == []
        assert conn.execute("SELECT * FROM blocks").fetchall() == []
        conn.execute("INSERT INTO task_history (task_id,timestamp,field,new_value) VALUES (1,'unchanged','blocks','[]')")
        for sql in ("UPDATE task_history SET new_value = 'erased'", "DELETE FROM task_history"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrate.migrate(db).backup is None and calls == [4]
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == checksum


def test_idea_and_block_sql_constraints(tmp_path):
    db = tmp_path / "constraints.db"
    shutil.copyfile(FIXTURE, db)
    migrate.migrate(db)
    with closing(connect(db)) as conn, conn:
        conn.execute("INSERT INTO ideas (title,project_id,created_at,updated_at) VALUES ('Herb garden',1,'same','same')")
        for sql in ("UPDATE ideas SET title = ' '", "UPDATE ideas SET status = 'active'", "UPDATE ideas SET project_id = 999"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        insert = "INSERT INTO blocks (task_id,idea_id,kind,position,done,done_at,created_at,updated_at) VALUES (?,?,?,?,?,?,'same','same')"
        for values in ((None,None,'note',0,0,None), (1,1,'note',0,0,None), (999,None,'note',0,0,None),
                       (None,999,'note',0,0,None), (1,None,'task',0,0,None), (1,None,'note',0,1,'same'),
                       (1,None,'note',0,0,'same'), (1,None,'subtask',0,1,None), (1,None,'subtask',0,0,'same'),
                       (1,None,'subtask',0,2,'same'), (1,None,'note',-1,0,None)):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert, values)
        conn.execute(insert, (1,None,'subtask',0,1,'same'))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (1,None,'note',0,0,None))
        conn.execute("DELETE FROM projects WHERE id = 1")
        assert conn.execute("SELECT project_id FROM ideas").fetchone()[0] is None
