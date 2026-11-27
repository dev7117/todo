"""Project REST contract, scoped leaf progress, assignment audit and restart safety."""
from contextlib import closing
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from todo_v2 import config, migrate
from todo_v2.app import create_app
from todo_v2.db import connect
from todo_v2.projects import progress


@pytest.fixture
def cfg(tmp_path):
    return replace(config.load(), db_path=tmp_path / "projects.db", frontend_dist=tmp_path / "dist")


@pytest.fixture
def client(cfg):
    with TestClient(create_app(cfg)) as client:
        yield client


def project(client, title="Kitchen refresh", **fields):
    response = client.post("/api/projects", json={"title": title, **fields})
    assert response.status_code == 201
    return response.json()


def task(client, title="Choose cabinet handles", project_id=None, parent_id=None, status="todo"):
    row = client.post("/api/tasks", json={"title": title, "parent_id": parent_id}).json()
    response = client.patch(f'/api/tasks/{row["id"]}', json={"project_id": project_id, "status": status})
    assert response.status_code == 200
    return response.json()


def test_project_round_trip(client):
    assert client.get("/api/projects").json() == []
    row = project(client, "  Kitchen refresh  ")
    assert row["title"] == "Kitchen refresh"
    assert row["status"] == "active"
    assert row["description"] == ""
    assert row["target_on"] is row["legacy_id"] is None
    assert row["created_at"] == row["updated_at"]
    assert row["progress"] == {"done": 0, "total": 0, "percent": 0}
    path = f'/api/projects/{row["id"]}'
    assert client.get(path).json() == {**row, "tasks": []}
    for status in ("paused", "done", "archived", "active"):
        changed = client.patch(path, json={"title": "Kitchen lighting", "description": "**Warm** bulbs",
                                         "status": status, "target_on": "2026-12-10"}).json()
        assert changed["status"] == status
        assert changed["title"] == "Kitchen lighting"
        assert changed["description"] == "**Warm** bulbs"
        assert changed["target_on"] == "2026-12-10"
        assert changed["created_at"] == row["created_at"]
        assert changed["updated_at"] > row["updated_at"]
        assert client.patch(path, json={"status": status}).json() == changed
        assert client.patch(path, json={}).json() == changed
    assert client.patch(path, json={"target_on": None, "description": ""}).json()["target_on"] is None
    assert client.get("/api/projects").json() == [{k: v for k, v in client.get(path).json().items() if k != "tasks"}]
    assert client.delete(path).status_code in (404, 405)
    assert client.get(path).status_code == 200
    health = client.get("/health").json()
    assert set(health) == {"ok", "version", "commit", "schema_version", "github"}
    assert health["schema_version"] == migrate.discover()[-1].version


@pytest.mark.parametrize("fields", [
    {"title": ""}, {"title": " \t\n"}, {"title": "\u2003"}, {"title": None},
    {"status": "cancelled"}, {"status": None}, {"description": None},
    {"target_on": "2026-02-30"}, {"target_on": "20261210"}, {"target_on": 123},
    {"goal_id": 1},
])
def test_project_validation(client, fields):
    assert client.post("/api/projects", json={"title": "Kitchen refresh", **fields}).status_code == 422
    row = project(client)
    path = f'/api/projects/{row["id"]}'
    assert client.patch(path, json=fields).status_code == 422
    assert client.get(path).json() == {**row, "tasks": []}


def test_missing_project_and_title(client):
    assert client.post("/api/projects", json={}).status_code == 422
    assert client.get("/api/projects/999").status_code == 404
    assert client.patch("/api/projects/999", json={"title": "Missing"}).status_code == 404
    assert client.patch("/api/projects/999", json={}).status_code == 404


def test_project_order(client):
    rows = [project(client, status=status) for status in ("active", "paused", "archived", "active")]
    assert client.patch(f'/api/projects/{rows[0]["id"]}', json={"status": "done"}).status_code == 200
    assert [p["id"] for p in client.get("/api/projects").json()] == [rows[i]["id"] for i in (1, 3, 0, 2)]


def test_assignment_history_noop_and_invalid_atomicity(client):
    first, second = project(client), project(client, "Winterize the shed")
    row = task(client)
    path = f'/api/tasks/{row["id"]}'
    initial = client.get(path + "/history").json()
    assert row["project_id"] is None
    for project_id in (first["id"], second["id"], None):
        changed = client.patch(path, json={"project_id": project_id}).json()
        assert changed["project_id"] == project_id
        assert changed["updated_at"] > row["updated_at"]
        history = client.get(path + "/history").json()
        assert history[-1]["field"] == "project_id"
        assert history[-1]["timestamp"] == changed["updated_at"]
        assert history[-1]["old_value"] == (str(row["project_id"]) if row["project_id"] else None)
        assert history[-1]["new_value"] == (str(project_id) if project_id else None)
        assert client.patch(path, json={"project_id": project_id}).json() == changed
        assert client.get(path + "/history").json() == history
        row = changed
    assert len(history) == len(initial) + 3
    for invalid in (999, 0, -1, True, "1", 1.5):
        assert client.patch(path, json={"project_id": invalid, "title": "Must not persist"}).status_code == 422
    assert client.get("/api/tasks").json() == [row]
    assert client.get(path + "/history").json() == history


def test_progress_counts_only_project_leaves(client, cfg):
    first, other, empty = project(client), project(client, "Shed"), project(client, "Garden")
    task(client, project_id=first["id"], status="done")
    task(client, project_id=first["id"], status="waiting")
    parent = task(client, project_id=first["id"], status="done")
    task(client, project_id=first["id"], parent_id=parent["id"], status="done")
    task(client, project_id=first["id"], parent_id=parent["id"], status="in_progress")
    task(client, project_id=first["id"], status="cancelled")
    task(client, project_id=other["id"], status="done")
    task(client, status="done")
    # A child in another project does not replace this project's parent.
    cross = task(client, project_id=first["id"], status="done")
    task(client, parent_id=cross["id"], project_id=other["id"])
    with closing(connect(cfg.db_path)) as conn:
        assert progress(conn, first["id"]) == {"done": 3, "total": 5, "percent": 60}
        assert progress(conn, empty["id"]) == {"done": 0, "total": 0, "percent": 0}
    detail = client.get(f'/api/projects/{first["id"]}').json()
    assert detail["progress"] == {"done": 3, "total": 5, "percent": 60}
    assert all(t["project_id"] == first["id"] for t in detail["tasks"])
    task(client, project_id=empty["id"], status="cancelled")
    assert client.get(f'/api/projects/{empty["id"]}').json()["progress"] == {"done": 0, "total": 0, "percent": 0}


def test_parent_completion_leaves_children_alone_and_cancelled_is_not_done(client):
    p = project(client)
    parent = task(client, project_id=p["id"])
    child = task(client, project_id=p["id"], parent_id=parent["id"])
    cancelled = task(client, project_id=p["id"], parent_id=parent["id"], status="cancelled")
    child_history = client.get(f'/api/tasks/{child["id"]}/history').json()
    completed = client.post(f'/api/tasks/{parent["id"]}/complete').json()
    assert completed["status"] == "done"
    detail = client.get(f'/api/projects/{p["id"]}').json()
    assert child in detail["tasks"] and cancelled in detail["tasks"]
    assert cancelled["completed_at"] is None
    assert client.get(f'/api/tasks/{child["id"]}/history').json() == child_history
    assert detail["progress"] == {"done": 0, "total": 1, "percent": 0}


def test_only_ui_rest_can_complete_projects(cfg):
    app = create_app(cfg)
    # Pin the full API surface: introducing an agent or MCP route (including a
    # nested ASGI mount) requires an explicit review of this completion contract.
    def flattened(router):
        for route in router.routes:
            if hasattr(route, "original_router"):
                yield from flattened(route.original_router)
            else:
                yield route

    routes = list(flattened(app))
    assert not any(isinstance(route, Mount) and route.path != "/assets" for route in routes)
    api_paths = {route.path for route in routes if route.path.startswith("/api/")}
    assert api_paths == {"/api/tasks", "/api/tasks/{task_id}", "/api/tasks/{task_id}/complete",
                         "/api/tasks/{task_id}/reopen", "/api/tasks/{task_id}/history", "/api/from-v1",
                         "/api/projects", "/api/projects/{project_id}",
                         "/api/ideas", "/api/ideas/{idea_id}",
                         "/api/tasks/{task_id}/blocks", "/api/ideas/{idea_id}/blocks",
                         "/api/tasks/{task_id}/blocks/order", "/api/ideas/{idea_id}/blocks/order",
                         "/api/blocks/{block_id}",
                         "/api/agent-work", "/api/agent-work/refresh"}
    assert not any("mcp" in route.path.lower() for route in routes)
    assert {(route.path, method) for route in routes if "agent" in route.path.lower()
            for method in route.methods} == {("/api/agent-work", "GET"), ("/api/agent-work/refresh", "POST")}
    with TestClient(app) as client:
        row = project(client)
        path = f'/api/projects/{row["id"]}'
        before = client.get(path).json()
        # The landed GitHub cache refresh is read-only with respect to projects.
        client.post("/api/agent-work/refresh", json={"project_id": row["id"], "status": "done"})
        assert client.get(path).json() == before
        assert client.post("/api/agent-work/complete", json={"project_id": row["id"]}).status_code in (404, 405)
    assert not any("mcp" in key.lower() for key in app.state._state)


def test_project_and_assignment_survive_restart(cfg):
    with TestClient(create_app(cfg)) as client:
        p = project(client, target_on="2026-12-10", description="Paint and hardware")
        t = task(client, project_id=p["id"])
        before = client.get(f'/api/projects/{p["id"]}').json()
        history = client.get(f'/api/tasks/{t["id"]}/history').json()
    with TestClient(create_app(cfg)) as client:
        assert client.get(f'/api/projects/{p["id"]}').json() == before
        assert client.get("/api/tasks").json() == [t]
        assert client.get(f'/api/tasks/{t["id"]}/history').json() == history


def test_subtask_inherits_project_at_creation_and_counts_in_progress(client):
    import json

    p = project(client)
    parent = task(client, project_id=p["id"], status="done")
    response = client.post("/api/tasks", json={"title": "Paint cabinet doors", "parent_id": parent["id"]})
    assert response.status_code == 201
    child = response.json()
    assert child["project_id"] == p["id"]
    history = client.get(f'/api/tasks/{child["id"]}/history').json()
    assert len(history) == 1 and history[0]["field"] == "create"
    assert json.loads(history[0]["new_value"]) == child
    detail = client.get(f'/api/projects/{p["id"]}').json()
    assert child in detail["tasks"]
    assert detail["progress"] == {"done": 0, "total": 1, "percent": 0}
    client.post(f'/api/tasks/{child["id"]}/complete')
    assert client.get(f'/api/projects/{p["id"]}').json()["progress"] == {"done": 1, "total": 1, "percent": 100}
    # Inheritance is at creation only; later parent reassignment does not cascade.
    client.patch(f'/api/tasks/{parent["id"]}', json={"project_id": None})
    assert client.get(f'/api/projects/{p["id"]}').json()["tasks"][0]["id"] == child["id"]
    unassigned = client.post("/api/tasks", json={"title": "Compare wood finishes", "parent_id": parent["id"]}).json()
    assert unassigned["project_id"] is None
    # Keep the create contract small: explicit assignment remains PATCH-only.
    assert client.post("/api/tasks", json={"title": "Choose a finish", "parent_id": parent["id"],
                                          "project_id": p["id"]}).status_code == 422


def test_parent_with_only_cancelled_children_counts_as_leaf(client, cfg):
    p = project(client)
    parent = task(client, project_id=p["id"])
    for title in ("Replace cabinet hinges", "Replace cabinet doors"):
        task(client, title, project_id=p["id"], parent_id=parent["id"], status="cancelled")
    with closing(connect(cfg.db_path)) as conn:
        assert progress(conn, p["id"]) == {"done": 0, "total": 1, "percent": 0}
    client.post(f'/api/tasks/{parent["id"]}/complete')
    with closing(connect(cfg.db_path)) as conn:
        assert progress(conn, p["id"]) == {"done": 1, "total": 1, "percent": 100}
    client.patch(f'/api/tasks/{parent["id"]}', json={"status": "cancelled"})
    with closing(connect(cfg.db_path)) as conn:
        assert progress(conn, p["id"]) == {"done": 0, "total": 0, "percent": 0}


def test_project_cannot_be_created_done_but_can_be_completed_later(client):
    assert client.post("/api/projects", json={"title": "Kitchen refresh", "status": "done"}).status_code == 422
    assert client.get("/api/projects").json() == []
    row = project(client)
    assert client.patch(f'/api/projects/{row["id"]}', json={"status": "done"}).json()["status"] == "done"
