"""Ideas and blocks contracts, atomic history, progress exclusions and restart."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from todo_v2 import config
from todo_v2.app import create_app
from todo_v2.db import connect


@pytest.fixture
def cfg(tmp_path):
    return replace(config.load(), db_path=tmp_path / "ideas.db", frontend_dist=tmp_path / "dist")


@pytest.fixture
def client(cfg):
    with TestClient(create_app(cfg)) as client:
        yield client


def create(client, owner="ideas", **fields):
    response = client.post(f"/api/{owner}", json={"title": "A kitchen window herb garden", **fields})
    assert response.status_code == 201
    return response.json()


def add(client, path, kind="note", body="**Basil** likes a sunny window."):
    response = client.post(path, json={"kind": kind, "body": body})
    assert response.status_code == 201
    return response.json()


def test_ideas_crud_and_filters(client, cfg):
    assert client.get("/api/ideas").json() == []
    p = create(client, "projects")
    row = create(client, title="  Window garden  ")
    path = f'/api/ideas/{row["id"]}'
    assert row["title"] == "Window garden"
    assert row["status"] == "seed" and row["summary"] == ""
    assert row["project_id"] is row["legacy_id"] is None
    assert row["created_at"] == row["updated_at"]
    assert client.get(path).json() == {**row, "blocks": []}
    other = create(client, status="parked", project_id=p["id"])
    assert client.get("/api/ideas").json() == [row, other]
    assert client.get("/api/ideas", params={"status": "parked"}).json() == [other]
    assert client.get("/api/ideas", params={"project_id": p["id"]}).json() == [other]
    assert client.get("/api/ideas", params={"status": "seed", "project_id": p["id"]}).json() == []
    assert client.get("/api/ideas?status=active").status_code == 422
    projects = client.get("/api/projects").json()
    for status in ("exploring", "parked", "promoted", "dropped", "seed"):
        updated = client.patch(path, json={"title": "Herbs by the window", "summary": "Start with basil.",
                                           "status": status, "project_id": p["id"]}).json()
        assert updated["status"] == status and updated["project_id"] == p["id"]
        assert updated["title"] == "Herbs by the window" and updated["summary"] == "Start with basil."
        assert updated["created_at"] == row["created_at"]
        assert updated["updated_at"] > row["updated_at"]
        assert client.patch(path, json={"status": status}).json() == updated
        assert client.patch(path, json={}).json() == updated
    assert client.patch(path, json={"project_id": None}).json()["project_id"] is None
    assert client.get("/api/tasks").json() == []
    assert client.get("/api/projects").json() == projects
    assert client.delete(path).status_code in (404, 405)
    assert client.post(path + "/promote").status_code in (404, 405)
    assert client.get(path).status_code == 200
    with closing(connect(cfg.db_path)) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'idea_history'").fetchall()
        assert conn.execute("SELECT * FROM task_history").fetchall() == []
    assert client.get("/health").json()["schema_version"] == 5


@pytest.mark.parametrize("fields", [
    {"title": ""}, {"title": " \t\n"}, {"title": "\u2003"}, {"title": None},
    {"summary": None}, {"status": "todo"}, {"status": None}, {"project_id": 999},
    {"project_id": True}, {"project_id": 0}, {"project_id": "1"}, {"goal_id": 1},
])
def test_idea_validation(client, fields):
    row = create(client)
    assert client.post("/api/ideas", json={"title": "Herbs", **fields}).status_code == 422
    path = f'/api/ideas/{row["id"]}'
    assert client.patch(path, json=fields).status_code == 422
    assert client.get(path).json() == {**row, "blocks": []}


def test_missing_idea(client):
    assert client.post("/api/ideas", json={}).status_code == 422
    assert client.get("/api/ideas/999").status_code == 404
    assert client.patch("/api/ideas/999", json={}).status_code == 404
    assert client.patch("/api/ideas/999", json={"title": "Herbs"}).status_code == 404


@pytest.mark.parametrize("owner", ["tasks", "ideas"])
def test_blocks_roundtrip_and_history(client, cfg, owner):
    row = create(client, owner)
    path = f'/api/{owner}/{row["id"]}/blocks'
    audit = f'/api/tasks/{row["id"]}/history'
    assert client.get(path).json() == []
    note = add(client, path)
    check = add(client, path, "subtask", "Measure the windowsill")
    assert [b["position"] for b in client.get(path).json()] == [0, 1]
    assert note["kind"] == "note" and not note["done"] and note["done_at"] is None
    assert "<strong>Basil</strong>" in note["body_html"]
    block_path = f'/api/blocks/{check["id"]}'
    updated = client.patch(block_path, json={"body": "Measure twice"}).json()
    assert updated["body"] == "Measure twice" and updated["updated_at"] > check["updated_at"]
    done = client.patch(block_path, json={"done": True}).json()
    assert done["done"] and done["done_at"] == done["updated_at"]
    assert client.patch(block_path, json={"done": True}).json() == done
    assert client.patch(block_path, json={}).json() == done
    reopened = client.patch(block_path, json={"done": False}).json()
    assert not reopened["done"] and reopened["done_at"] is None
    order = client.put(path + "/order", json={"ids": [check["id"], note["id"]]})
    assert order.status_code == 200
    assert [b["id"] for b in order.json()] == [check["id"], note["id"]]
    assert [b["position"] for b in order.json()] == [0, 1]
    assert client.put(path + "/order", json={"ids": [check["id"], note["id"]]}).json() == order.json()
    if owner == "ideas":
        assert client.get(f'/api/ideas/{row["id"]}').json()["blocks"] == order.json()
    assert client.delete(f'/api/blocks/{note["id"]}').status_code == 204
    assert client.patch(f'/api/blocks/{note["id"]}', json={"body": "gone"}).status_code == 404
    final = client.get(path).json()
    assert len(final) == 1 and final[0]["id"] == check["id"]
    with closing(connect(cfg.db_path)) as conn:
        history = [dict(r) for r in conn.execute("SELECT * FROM task_history WHERE field = 'blocks' ORDER BY id")]
        assert len(history) == (7 if owner == "tasks" else 0)
        if history:
            assert json.loads(history[0]["old_value"]) == []
            for previous, following in zip(history, history[1:]):
                assert previous["new_value"] == following["old_value"]
            assert json.loads(history[-1]["new_value"])[0]["id"] == check["id"]
            assert conn.execute("SELECT updated_at FROM tasks WHERE id = ?", (row["id"],)).fetchone()[0] == history[-1]["timestamp"]
            assert len(client.get(audit).json()) == 8


@pytest.mark.parametrize("owner", ["tasks", "ideas"])
def test_blocks_invalid_and_owner_isolation(client, owner):
    row = create(client, owner)
    path = f'/api/{owner}/{row["id"]}/blocks'
    note = add(client, path)
    other = create(client, owner)
    foreign = add(client, f'/api/{owner}/{other["id"]}/blocks')
    for fields in ({"kind": "task"}, {"kind": "note", "done": True}, {"kind": "note", "position": 10}):
        assert client.post(path, json=fields).status_code == 422
    for fields in ({"done": True}, {"done": False}, {"done": None}, {"body": None},
                   {"kind": "subtask"}, {"task_id": 999}, {"done_at": "today"}):
        assert client.patch(f'/api/blocks/{note["id"]}', json=fields).status_code == 422
    for ids in ([], [note["id"], note["id"]], [foreign["id"]], [999], [True], [str(note["id"])]):
        assert client.put(path + "/order", json={"ids": ids}).status_code == 422
    assert client.get(path).json() == [note]
    missing = f'/api/{owner}/999/blocks'
    assert client.get(missing).status_code == 404
    assert client.post(missing, json={"kind": "note"}).status_code == 404
    assert client.put(missing + "/order", json={"ids": []}).status_code == 404
    assert client.patch("/api/blocks/999", json={"body": "missing"}).status_code == 404
    assert client.delete("/api/blocks/999").status_code == 404


def test_task_block_write_rolls_back_when_history_fails(client, cfg):
    task = create(client, "tasks")
    path = f'/api/tasks/{task["id"]}/blocks'
    with closing(connect(cfg.db_path)) as conn, conn:
        conn.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON task_history "
                     "WHEN NEW.field = 'blocks' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        add(client, path)
    assert client.get(path).json() == []
    assert client.get("/api/tasks").json() == [task]


def test_blocks_and_ideas_do_not_change_leaf_progress(client):
    p = create(client, "projects")
    def task(status="todo", parent=None):
        row = create(client, "tasks", parent_id=parent)
        client.patch(f'/api/tasks/{row["id"]}', json={"project_id": p["id"], "status": status})
        return row["id"]
    parent = task("done")
    task("done", parent)
    task("waiting", parent)  # done parent skipped, these two leaves count
    task("cancelled")
    all_cancelled = task("done")
    task("cancelled", all_cancelled)  # parent counts as a done leaf
    endpoint = f'/api/projects/{p["id"]}'
    before = client.get(endpoint).json()["progress"]
    assert before == {"done": 2, "total": 3, "percent": 200 / 3}
    path = f'/api/tasks/{parent}/blocks'
    add(client, path)
    check = add(client, path, "subtask")
    client.patch(f'/api/blocks/{check["id"]}', json={"done": True})
    idea = create(client, project_id=p["id"])
    another = create(client, status="promoted")
    client.patch(f'/api/ideas/{another["id"]}', json={"project_id": p["id"]})
    add(client, f'/api/ideas/{idea["id"]}/blocks', "subtask")
    assert client.get(endpoint).json()["progress"] == before


def test_restart_preserves_idea_blocks_and_history(cfg):
    with TestClient(create_app(cfg)) as client:
        idea = create(client)
        task = create(client, "tasks")
        idea_path = f'/api/ideas/{idea["id"]}'
        task_path = f'/api/tasks/{task["id"]}/blocks'
        add(client, idea_path + "/blocks")
        block = add(client, task_path, "subtask")
        client.patch(f'/api/blocks/{block["id"]}', json={"done": True})
        before = client.get(idea_path).json()
        blocks = client.get(task_path).json()
        history = client.get(f'/api/tasks/{task["id"]}/history').json()
    with TestClient(create_app(cfg)) as client:
        assert client.get(idea_path).json() == before
        assert client.get(task_path).json() == blocks
        assert client.get(f'/api/tasks/{task["id"]}/history').json() == history
        assert client.get("/health").json()["schema_version"] == 5
    assert list(cfg.db_path.parent.glob("*.bak")) == []
