"""Today uses an injected instant; literal FTS and audit survive independent boots."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from fastapi.testclient import TestClient

from todo_v2 import config, search
from todo_v2.app import create_app
from todo_v2.db import connect


@pytest.fixture
def cfg(tmp_path):
    return replace(config.load(), db_path=tmp_path / "today.db", frontend_dist=tmp_path / "dist",
                   timezone="America/Detroit")


@pytest.fixture
def clock():
    # UTC has crossed midnight; Detroit is still November 27.
    instant = [datetime(2026, 11, 28, 4, 59, tzinfo=timezone.utc)]
    return instant


@pytest.fixture
def client(cfg, clock):
    with TestClient(create_app(cfg, clock=lambda: clock[0])) as client:
        yield client


def add(client, title="Prepare the neighborhood workshop", **fields):
    response = client.post("/api/tasks", json={"title": title, **fields})
    assert response.status_code == 201
    return response.json()


def hits(client, query):
    response = client.get("/api/search", params={"q": query})
    assert response.status_code == 200
    return response.json()["hits"]


def test_quick_add_and_today_history(client):
    normal = add(client, "  Order workshop supplies  ")
    assert normal["title"] == "Order workshop supplies"
    assert normal["status"] == "todo" and normal["today_on"] is None
    today = add(client, today=True)
    assert today["status"] == "todo" and today["today_on"] == "2026-11-27" and today["is_today"]
    for title in ("", " \n\t", "\u2003"):
        assert client.post("/api/tasks", json={"title": title, "today": True}).status_code == 422
    path = f'/api/tasks/{today["id"]}'
    history = client.get(path + "/history").json()
    assert [row["field"] for row in history] == ["create", "today_on"]
    assert (history[-1]["old_value"], history[-1]["new_value"]) == (None, "2026-11-27")
    assert client.patch(path, json={"today": True}).json() == today
    assert client.get(path + "/history").json() == history
    cleared = client.patch(path, json={"today": False}).json()
    assert cleared["today_on"] is None and not cleared["is_today"]
    assert client.get("/api/tasks/today").json() == []
    last = client.get(path + "/history").json()[-1]
    assert (last["field"], last["old_value"], last["new_value"]) == ("today_on", "2026-11-27", None)
    assert client.patch(path, json={"today": True}).json()["today_on"] == "2026-11-27"
    assert client.patch(path, json={"today": None}).status_code == 422
    assert client.patch(path, json={"today": "yes"}).status_code == 422
    assert client.post("/api/tasks", json={"title": "Supplies", "today": 1}).status_code == 422


def test_today_read_boundary_never_clears_or_writes(client, cfg, clock):
    yesterday = add(client, "Yesterday's planning", today=True)
    clock[0] += timedelta(days=1)
    today = add(client, "Prepare chairs", today=True)
    done = add(client, "Confirm room", today=True)
    cancelled = add(client, "Reserve overflow room", today=True)
    client.patch(f'/api/tasks/{done["id"]}', json={"status": "done"})
    client.patch(f'/api/tasks/{cancelled["id"]}', json={"status": "cancelled"})
    clock[0] += timedelta(days=1)
    tomorrow = add(client, "Send the recap", today=True)
    clock[0] -= timedelta(days=1)
    with closing(connect(cfg.db_path)) as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
        history = [tuple(row) for row in conn.execute("SELECT * FROM task_history ORDER BY id")]
    assert [row["id"] for row in client.get("/api/tasks/today").json()] == [today["id"]]
    assert yesterday["today_on"] == "2026-11-27" and tomorrow["today_on"] == "2026-11-29"
    with closing(connect(cfg.db_path)) as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")] == before
        assert [tuple(row) for row in conn.execute("SELECT * FROM task_history ORDER BY id")] == history
    clock[0] += timedelta(minutes=1)  # Detroit midnight, with no server restart.
    assert [row["id"] for row in client.get("/api/tasks/today").json()] == [tomorrow["id"]]


def test_configured_timezone_and_project_today_fields(cfg, clock, monkeypatch):
    monkeypatch.setenv("TODO_V2_TZ", "UTC")
    assert config.load().timezone == "UTC"
    with TestClient(create_app(replace(cfg, timezone="UTC"), clock=lambda: clock[0])) as client:
        task = add(client, today=True)
        assert task["today_on"] == "2026-11-28"
        project = client.post("/api/projects", json={"title": "Workshop"}).json()
        client.patch(f'/api/tasks/{task["id"]}', json={"project_id": project["id"]})
        detail = client.get(f'/api/projects/{project["id"]}').json()
        assert detail["tasks"][0]["is_today"] is True


@pytest.mark.parametrize("query,matching,other", [
    ('"', None, "ordinary words"), ('*', None, "ordinary words"),
    ('NEAR', "Near the library", "library"), ('OR', "Tea or coffee", "coffee"),
    ('-', None, "ordinary words"), (':', None, "ordinary words"),
    ('tea OR coffee', "Tea or coffee", "tea"),
    ('notes:orchid', "notes orchid", "orchid"),
    ('orch*', "orch", "orchid"),
    ('tea -coffee', "Tea coffee", "tea"),
    ('"tea"', "tea", "coffee"),
    ('tea\x00coffee', 'Tea coffee', 'tea'),
])
def test_search_treats_operators_as_literal_text(client, query, matching, other):
    add(client, other)
    expected = add(client, matching) if matching else None
    found = hits(client, query)
    assert [hit["id"] for hit in found] == ([expected["id"]] if expected else [])


def test_search_sync_all_fields_and_owner_deleted_blocks(client, cfg):
    task = add(client, "Juniper planning", notes="Copperfinch supplies")
    idea = client.post("/api/ideas", json={"title": "Lantern evening", "summary": "Moonflower route"}).json()
    for term, owner, row in [("Juniper", "tasks", task), ("Copperfinch", "tasks", task),
                              ("Lantern", "ideas", idea), ("Moonflower", "ideas", idea)]:
        assert hits(client, term)[0]["url"] == f'/{owner}/{row["id"]}'
    client.patch(f'/api/tasks/{task["id"]}', json={"title": "Willow planning", "notes": "Goldfinch supplies"})
    client.patch(f'/api/ideas/{idea["id"]}', json={"title": "Starlight evening", "summary": "Sunflower route"})
    for term in ("Juniper", "Copperfinch", "Lantern", "Moonflower"):
        assert hits(client, term) == []
    for term in ("Willow", "Goldfinch", "Starlight", "Sunflower"):
        assert len(hits(client, term)) == 1
    for owner, row in (("tasks", task), ("ideas", idea)):
        path = f'/api/{owner}/{row["id"]}/blocks'
        block = client.post(path, json={"kind": "note", "body": "Bluejay arrival details"}).json()
        assert hits(client, "Bluejay")[0]["url"] == f'/{owner}/{row["id"]}'
        assert hits(client, "Bluejay")[0]["kind"] == "block"
        client.patch(f'/api/blocks/{block["id"]}', json={"body": "Chickadee arrival details"})
        assert hits(client, "Bluejay") == [] and len(hits(client, "Chickadee")) == 1
        # The owner's UI uses this existing block-delete route, including audit updates.
        assert client.delete(f'/api/blocks/{block["id"]}').status_code == 204
        assert client.get(path).json() == [] and hits(client, "Chickadee") == []
    history = client.get(f'/api/tasks/{task["id"]}/history').json()
    assert [row["field"] for row in history[-3:]] == ["blocks"] * 3
    # Tasks and ideas have no public delete route. Exercise their SQL delete triggers
    # with fresh rows that have no audit/owner dependencies.
    with closing(connect(cfg.db_path)) as conn, conn:
        for table in ("tasks", "ideas"):
            conn.execute(f"INSERT INTO {table} (title,created_at,updated_at) VALUES ('Firefly','same','same')")
    assert len(hits(client, "Firefly")) == 2
    with closing(connect(cfg.db_path)) as conn, conn:
        for table in ("tasks", "ideas"):
            conn.execute(f"DELETE FROM {table} WHERE title = 'Firefly'")
    assert hits(client, "Firefly") == []


def test_search_cap_and_blank_query(client):
    for i in range(search.RESULT_LIMIT + 3):
        add(client, f"Workshop supply {i}")
    result = client.get("/api/search", params={"q": "Workshop"}).json()
    assert len(result["hits"]) == result["limit"] == 50
    assert hits(client, "   ") == []
    assert client.get("/api/search", params={"q": "x" * 501}).status_code == 422


def test_restart_preserves_today_quick_add_and_searchable_block(cfg, clock):
    with TestClient(create_app(cfg, clock=lambda: clock[0])) as client:
        task = add(client, "Book the community room", today=True)
        add(client, "Return borrowed chairs")
        path = f'/api/tasks/{task["id"]}'
        client.post(path + "/blocks", json={"kind": "note", "body": "Silverleaf entrance code goes in the invitation."})
        before = client.get("/api/tasks").json()
        history = client.get(path + "/history").json()
    with TestClient(create_app(cfg, clock=lambda: clock[0])) as client:
        assert client.get("/api/tasks").json() == before
        assert client.get(path + "/history").json() == history
        assert client.get("/api/tasks/today").json()[0]["id"] == task["id"]
        assert hits(client, "Silverleaf")[0]["url"] == f'/tasks/{task["id"]}'
    assert list(cfg.db_path.parent.glob("*.bak")) == []


@pytest.mark.parametrize("existing", [False, True])
def test_missing_fts5_aborts_startup_even_without_pending_migrations(cfg, monkeypatch, existing):
    if existing:
        with TestClient(create_app(cfg)):
            pass
    before = cfg.db_path.read_bytes() if existing else None
    class MissingFTS:
        def execute(self, sql):
            raise sqlite3.OperationalError("no such module: fts5")
        def close(self):
            pass
    monkeypatch.setattr(search.sqlite3, "connect", lambda *args, **kwargs: MissingFTS())
    with pytest.raises(RuntimeError, match="requires SQLite FTS5"):
        with TestClient(create_app(cfg)):
            pass
    assert (cfg.db_path.read_bytes() if existing else None) == before
    assert cfg.db_path.exists() == existing
    assert list(cfg.db_path.parent.glob("*.bak")) == []
