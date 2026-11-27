"""Task API contract and persistence across independent app lifespans."""
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from todo_v2 import config, migrate
from todo_v2.app import create_app


@pytest.fixture
def cfg(tmp_path: Path):
    return replace(config.load(), db_path=tmp_path / "tasks.db", frontend_dist=tmp_path / "dist")


@pytest.fixture
def client(cfg):
    with TestClient(create_app(cfg)) as client:
        yield client


def test_round_trip(client):
    assert client.get("/api/tasks").json() == []
    response = client.post("/api/tasks", json={"title": "  Pick up oat milk  "})
    assert response.status_code == 201
    task = response.json()
    path = f'/api/tasks/{task["id"]}'
    assert task["title"] == "Pick up oat milk"
    assert task["notes"] == ""
    assert task["status"] == "todo"
    assert task["completed_at"] is None
    assert task["created_at"] == task["updated_at"]

    edited = client.patch(path, json={"title": "Pick up groceries", "notes": "Oat milk and apples"})
    assert edited.status_code == 200
    edited = edited.json()
    assert (edited["title"], edited["notes"]) == ("Pick up groceries", "Oat milk and apples")
    assert edited["created_at"] == task["created_at"]
    assert edited["updated_at"] >= task["updated_at"]
    assert client.patch(path, json={"title": "Shop on Friday"}).json()["notes"] == "Oat milk and apples"
    assert client.patch(path, json={"notes": ""}).json()["title"] == "Shop on Friday"
    assert client.patch(path, json={}).status_code == 200

    other = client.post("/api/tasks", json={"title": "Finish the tax packet"}).json()
    completed = client.post(path + "/complete")
    assert completed.status_code == 200
    completed = completed.json()
    assert completed["status"] == "done"
    assert completed["completed_at"] == completed["updated_at"]
    assert client.post(path + "/complete").json() == completed
    assert [row["id"] for row in client.get("/api/tasks").json()] == [other["id"], task["id"]]
    assert client.patch(path, json={"notes": "Receipt saved"}).json()["completed_at"] == completed["completed_at"]

    reopened = client.post(path + "/reopen")
    assert reopened.status_code == 200
    reopened = reopened.json()
    assert reopened["status"] == "todo"
    assert reopened["completed_at"] is None
    assert reopened["notes"] == "Receipt saved"
    assert client.post(path + "/reopen").json() == reopened


@pytest.mark.parametrize("title", ["", "   ", "\t\n", "\u2003", None])
def test_invalid_title_rejected_on_create_and_edit(client, title):
    assert client.post("/api/tasks", json={"title": title}).status_code == 422
    task = client.post("/api/tasks", json={"title": "Book a haircut"}).json()
    assert client.patch(f'/api/tasks/{task["id"]}', json={"title": title}).status_code == 422
    assert client.get("/api/tasks").json() == [task]


def test_missing_title_and_unsupported_fields(client):
    assert client.post("/api/tasks", json={"notes": "No title"}).status_code == 422
    assert client.post("/api/tasks", json={"title": "Task", "status": "done"}).status_code == 422
    task = client.post("/api/tasks", json={"title": "Book a haircut"}).json()
    assert client.patch(f'/api/tasks/{task["id"]}', json={"unknown": "value"}).status_code == 422


@pytest.mark.parametrize("method,suffix,body", [
    ("PATCH", "", {"title": "Missing"}), ("PATCH", "", {}),
    ("GET", "/history", None), ("POST", "/complete", None), ("POST", "/reopen", None),
])
def test_missing_task_is_404(client, method, suffix, body):
    assert client.request(method, "/api/tasks/999" + suffix, json=body).status_code == 404


def test_tasks_survive_restart(cfg):
    with TestClient(create_app(cfg)) as client:
        task = client.post("/api/tasks", json={"title": "Email the dentist", "notes": "Confirm Thursday"}).json()
        path = f'/api/tasks/{task["id"]}'
        assert client.patch(path, json={"title": "Confirm the appointment"}).status_code == 200
        assert client.post(path + "/complete").status_code == 200
        client.post("/api/tasks", json={"title": "Finish the tax packet"})
        child = client.post("/api/tasks", json={"title": "Find last year's receipts", "parent_id": task["id"]}).json()
        before = client.get("/api/tasks").json()
        history = client.get(path + "/history").json()
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/tasks").json() == before
        assert client.get(path + "/history").json() == history
        assert next(row for row in before if row["id"] == child["id"])["parent_id"] == task["id"]
        assert client.get("/health").json()["schema_version"] == migrate.discover()[-1].version
        assert client.post(path + "/reopen").json()["completed_at"] is None


def test_all_statuses_and_history(client):
    import json
    import re

    task = client.post("/api/tasks", json={"title": "Plan Sunday meals"}).json()
    path = f'/api/tasks/{task["id"]}'
    history = client.get(path + "/history").json()
    assert len(history) == 1
    assert history[0]["field"] == "create"
    assert history[0]["old_value"] is None
    assert json.loads(history[0]["new_value"]) == task
    assert history[0]["timestamp"] == task["created_at"]
    edited = client.patch(path, json={"title": "Plan next week's meals", "notes": "Check the pantry first"}).json()
    history = client.get(path + "/history").json()
    assert [(h["field"], h["old_value"], h["new_value"]) for h in history[1:]] == [
        ("title", task["title"], edited["title"]), ("notes", "", edited["notes"]),
    ]
    assert all(h["timestamp"] == edited["updated_at"] for h in history[1:])
    old_status = "todo"
    for status in ("inbox", "todo", "in_progress", "waiting", "done", "cancelled"):
        response = client.patch(path, json={"status": status})
        assert response.status_code == 200
        changed = response.json()
        assert changed["status"] == status
        assert changed["completed_at"] == (changed["updated_at"] if status == "done" else None)
        history = client.get(path + "/history").json()
        last = history[-1]
        assert (last["field"], last["old_value"], last["new_value"]) == ("status", old_status, status)
        assert last["timestamp"] == changed["updated_at"]
        assert client.patch(path, json={"status": status}).json() == changed
        assert client.patch(path, json={"title": changed["title"], "notes": changed["notes"]}).json() == changed
        assert client.patch(path, json={}).json() == changed
        assert client.get(path + "/history").json() == history
        old_status = status
    assert [h["id"] for h in history] == sorted(h["id"] for h in history)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", h["timestamp"]) for h in history)
    assert client.post(path + "/reopen").json()["status"] == "todo"
    assert client.post(path + "/complete").json()["status"] == "done"
    history = client.get(path + "/history").json()
    assert client.post(path + "/complete").status_code == 200
    assert client.get(path + "/history").json() == history
    assert [h["new_value"] for h in history[-2:]] == ["todo", "done"]


@pytest.mark.parametrize("body", [
    {"status": "open"}, {"status": "unknown"}, {"status": ""}, {"status": None},
    {"notes": None}, {"parent_id": 1.5}, {"parent_id": True}, {"parent_id": 0},
])
def test_invalid_patch_is_atomic(client, body):
    task = client.post("/api/tasks", json={"title": "Book a haircut"}).json()
    path = f'/api/tasks/{task["id"]}'
    history = client.get(path + "/history").json()
    assert client.patch(path, json=body).status_code == 422
    assert client.get("/api/tasks").json() == [task]
    assert client.get(path + "/history").json() == history


def test_parenting_and_cycles(client):
    parent = client.post("/api/tasks", json={"title": "Finish the tax packet"}).json()
    child = client.post("/api/tasks", json={"title": "Collect receipts", "parent_id": parent["id"]}).json()
    grandchild = client.post("/api/tasks", json={"title": "Find the donation receipt", "parent_id": child["id"]}).json()
    assert child["parent_id"] == parent["id"]
    assert grandchild["parent_id"] == child["id"]
    path = f'/api/tasks/{parent["id"]}'
    before = client.get("/api/tasks").json()
    history = client.get(path + "/history").json()
    for invalid in (parent["id"], child["id"], grandchild["id"]):
        assert client.patch(path, json={"parent_id": invalid, "title": "Must not persist"}).status_code == 422
    assert client.patch(path, json={"parent_id": 999}).status_code == 404
    assert client.post("/api/tasks", json={"title": "Orphan", "parent_id": 999}).status_code == 404
    assert client.get("/api/tasks").json() == before
    assert client.get(path + "/history").json() == history
    path = f'/api/tasks/{grandchild["id"]}'
    assert client.patch(path, json={"parent_id": parent["id"]}).json()["parent_id"] == parent["id"]
    assert client.patch(path, json={"parent_id": None}).json()["parent_id"] is None
    changes = client.get(path + "/history").json()[1:]
    assert [(h["field"], h["old_value"], h["new_value"]) for h in changes] == [
        ("parent_id", str(child["id"]), str(parent["id"])), ("parent_id", str(parent["id"]), None),
    ]


def test_active_first_and_stable_within_groups(client):
    ids = []
    statuses = ["done", "waiting", "cancelled", "inbox", "in_progress", "todo"]
    for status in statuses:
        task = client.post("/api/tasks", json={"title": f"Plan a {status} errand"}).json()
        ids.append(task["id"])
        client.patch(f'/api/tasks/{task["id"]}', json={"status": status})
    assert [task["id"] for task in client.get("/api/tasks").json()] == [ids[i] for i in (1, 3, 4, 5, 0, 2)]
    assert client.get("/api/items").status_code == 404


def test_concurrent_parent_edits_cannot_create_a_cycle(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    first = client.post("/api/tasks", json={"title": "Plan the garden"}).json()
    second = client.post("/api/tasks", json={"title": "Order seeds"}).json()
    ready = Barrier(2)

    def parent(task, other):
        ready.wait(timeout=5)
        return client.patch(f'/api/tasks/{task["id"]}', json={"parent_id": other["id"]})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(parent, first, second), pool.submit(parent, second, first)]
        assert sorted(f.result().status_code for f in futures) == [200, 422]
    rows = client.get("/api/tasks").json()
    assert sum(row["parent_id"] is not None for row in rows) == 1
    histories = [client.get(f'/api/tasks/{row["id"]}/history').json() for row in rows]
    assert sum(h["field"] == "parent_id" for history in histories for h in history) == 1


def test_failed_write_rolls_back_history(client, cfg):
    from contextlib import closing
    import sqlite3
    from todo_v2.db import connect

    task = client.post("/api/tasks", json={"title": "Plan the garden"}).json()
    path = f'/api/tasks/{task["id"]}'
    history = client.get(path + "/history").json()
    with closing(connect(cfg.db_path)) as conn:
        conn.executescript("CREATE TRIGGER reject_task_update BEFORE UPDATE ON tasks "
                           "BEGIN SELECT RAISE(ABORT, 'Write failed'); END;")
    with pytest.raises(sqlite3.IntegrityError, match="Write failed"):
        client.patch(path, json={"title": "Order seeds", "status": "waiting"})
    assert client.get("/api/tasks").json() == [task]
    assert client.get(path + "/history").json() == history
