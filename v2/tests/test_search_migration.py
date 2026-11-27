"""Copy invented schema-5 data; preserve its source and back up exactly once."""
from contextlib import closing
from dataclasses import replace
import hashlib
from pathlib import Path
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient
from todo_v2 import config, migrate
from todo_v2.app import create_app
from todo_v2.db import connect

FIXTURE = Path(__file__).parent / "fixtures" / "ideas-v5.sqlite3"


def snapshot(conn):
    return {table: [{key: row[key] for key in row.keys() if key != "today_on"} for row in
                    conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("tasks", "projects", "task_history", "ideas", "blocks")}


def test_schema_five_backfill_backup_preservation(tmp_path, monkeypatch):
    checksum = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert checksum == "6cfd989ad5664454d2b272cf700fe65dceb98acc48a483f7b019b21dd7b368e5"
    db = tmp_path / "search.db"
    shutil.copyfile(FIXTURE, db)
    with closing(connect(db)) as conn:
        assert migrate.schema_version(conn) == 5
        before = snapshot(conn)
        assert all(before.values())
    original = migrate.backup
    calls = []
    def backup(conn, path, current):
        assert current == 5 and snapshot(conn) == before
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'tasks_fts'").fetchone() is None
        calls.append(current)
        return original(conn, path, current)
    monkeypatch.setattr(migrate, "backup", backup)
    result = migrate.migrate(db)
    assert result.applied == [6] and result.schema_version == migrate.discover()[-1].version
    assert calls == [5] and result.backup is not None
    assert len(list(tmp_path.glob("*.bak"))) == 1
    with closing(connect(result.backup)) as conn:
        assert migrate.schema_version(conn) == 5 and snapshot(conn) == before
    with closing(connect(db)) as conn, conn:
        assert snapshot(conn) == before
        assert all(row[0] is None for row in conn.execute("SELECT today_on FROM tasks"))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        conn.execute("INSERT INTO task_history (task_id,timestamp,field,new_value) VALUES (1,'same','today_on','2026-11-27')")
        for sql in ("UPDATE task_history SET new_value = 'erased'", "DELETE FROM task_history"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in ("tasks", "ideas", "blocks"):
            conn.execute(f"INSERT INTO {table}_fts({table}_fts, rank) VALUES ('integrity-check', 1)")
    cfg = replace(config.load(), db_path=db, frontend_dist=tmp_path / "dist")
    with TestClient(create_app(cfg)) as client:
        for term, url, kind in (("workshop", "/tasks/1", "task"), ("Copperfinch", "/tasks/1", "task"),
                                ("Lantern", "/ideas/1", "idea"), ("Moonflower", "/ideas/1", "idea"),
                                ("Silverleaf", "/tasks/1", "block"), ("Bluejay", "/ideas/1", "block")):
            hit = client.get("/api/search", params={"q": term}).json()["hits"][0]
            assert (hit["url"], hit["kind"]) == (url, kind)
    assert migrate.migrate(db).backup is None and calls == [5]
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == checksum
