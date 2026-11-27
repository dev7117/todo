"""Committed fixtures contain only invented titles and all six v1 task statuses."""
from contextlib import closing
from dataclasses import replace
import hashlib
from pathlib import Path
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from todo_v2 import config
from todo_v2.app import create_app

FIXTURES = Path(__file__).parent / "fixtures"
OPEN = ["Plan a weekend walk", "Pick up a birthday card", "Sort the recipe folder", "Confirm the repair estimate"]


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_view(tmp_path, source):
    cfg = replace(config.load(), db_path=tmp_path / "v2.db", v1_db_path=source)
    with TestClient(create_app(cfg)) as client:
        return client.get("/api/from-v1")


@pytest.mark.parametrize("name", ["v1.sqlite3", "v1-wal.sqlite3"])
def test_fixture_titles_and_checksum_unchanged(tmp_path, name):
    source = FIXTURES / name
    before = checksum(source)
    files_before = set(FIXTURES.iterdir())
    if "wal" in name:
        assert source.read_bytes()[18:20] == b"\x02\x02"  # persistent WAL header
    response = read_view(tmp_path, source)
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "items": OPEN}
    assert checksum(source) == before
    assert set(FIXTURES.iterdir()) == files_before  # no sidecar files


@pytest.mark.parametrize("source", [None, "missing.sqlite3"])
def test_unset_or_missing_is_hidden(tmp_path, source):
    path = tmp_path / source if source else None
    response = read_view(tmp_path, path)
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "items": []}
    if path:
        assert not path.exists()


def test_read_only_directory_and_uri_characters(tmp_path):
    directory = tmp_path / "read only"
    directory.mkdir()
    source = directory / "v1 #snapshot?.sqlite3"
    shutil.copyfile(FIXTURES / "v1-wal.sqlite3", source)
    source.chmod(0o444)
    directory.chmod(0o555)
    try:
        before = checksum(source)
        assert read_view(tmp_path, source).json() == {"enabled": True, "items": OPEN}
        assert checksum(source) == before
        assert list(directory.iterdir()) == [source]
    finally:
        directory.chmod(0o755)
        source.chmod(0o644)


def test_pending_wal_is_ignored_without_touching_files(tmp_path):
    directory = tmp_path / "legacy"
    directory.mkdir()
    source = directory / "v1.sqlite3"
    shutil.copyfile(FIXTURES / "v1-wal.sqlite3", source)
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO tasks (title, status) VALUES ('Only in the pending WAL', 'todo')")
        writer.commit()
        assert Path(str(source) + "-wal").stat().st_size > 0
        before = {path.name: checksum(path) for path in directory.iterdir()}
        # Documents immutable's deliberate limitation: checkpointed contents only.
        assert read_view(tmp_path, source).json() == {"enabled": True, "items": OPEN}
        assert {path.name: checksum(path) for path in directory.iterdir()} == before


def test_invalid_snapshot_does_not_expose_path(tmp_path):
    source = tmp_path / "unreadable-snapshot.sqlite3"
    source.write_text("not a database")
    response = read_view(tmp_path, source)
    assert response.status_code == 503
    assert response.json() == {"detail": "The v1 snapshot could not be read"}


def test_config_opt_in(monkeypatch):
    monkeypatch.delenv("TODO_V2_V1_DB", raising=False)
    assert config.load().v1_db_path is None
    monkeypatch.setenv("TODO_V2_V1_DB", "")
    assert config.load().v1_db_path is None
    monkeypatch.setenv("TODO_V2_V1_DB", "/path/to/todo.db")
    assert config.load().v1_db_path == Path("/path/to/todo.db")
