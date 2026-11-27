"""Item API contract and persistence across independent app lifespans."""
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from todo_v2 import config, migrate
from todo_v2.app import create_app


@pytest.fixture
def cfg(tmp_path: Path):
    return replace(config.load(), db_path=tmp_path / "items.db", frontend_dist=tmp_path / "dist")


@pytest.fixture
def client(cfg):
    with TestClient(create_app(cfg)) as client:
        yield client


def test_round_trip(client):
    assert client.get("/api/items").json() == []
    response = client.post("/api/items", json={"title": "  Pick up oat milk  "})
    assert response.status_code == 201
    item = response.json()
    path = f'/api/items/{item["id"]}'
    assert item["title"] == "Pick up oat milk"
    assert item["notes"] == ""
    assert item["status"] == "open"
    assert item["completed_at"] is None
    assert item["created_at"] == item["updated_at"]

    edited = client.patch(path, json={"title": "Pick up groceries", "notes": "Oat milk and apples"})
    assert edited.status_code == 200
    edited = edited.json()
    assert (edited["title"], edited["notes"]) == ("Pick up groceries", "Oat milk and apples")
    assert edited["created_at"] == item["created_at"]
    assert edited["updated_at"] >= item["updated_at"]
    assert client.patch(path, json={"title": "Shop on Friday"}).json()["notes"] == "Oat milk and apples"
    assert client.patch(path, json={"notes": ""}).json()["title"] == "Shop on Friday"
    assert client.patch(path, json={}).status_code == 200

    other = client.post("/api/items", json={"title": "Finish the tax packet"}).json()
    completed = client.post(path + "/complete")
    assert completed.status_code == 200
    completed = completed.json()
    assert completed["status"] == "done"
    assert completed["completed_at"] == completed["updated_at"]
    assert client.post(path + "/complete").json() == completed
    assert [row["id"] for row in client.get("/api/items").json()] == [other["id"], item["id"]]
    assert client.patch(path, json={"notes": "Receipt saved"}).json()["completed_at"] == completed["completed_at"]

    reopened = client.post(path + "/reopen")
    assert reopened.status_code == 200
    reopened = reopened.json()
    assert reopened["status"] == "open"
    assert reopened["completed_at"] is None
    assert reopened["notes"] == "Receipt saved"
    assert client.post(path + "/reopen").json() == reopened


@pytest.mark.parametrize("title", ["", "   ", "\t\n", "\u2003", None])
def test_invalid_title_rejected_on_create_and_edit(client, title):
    assert client.post("/api/items", json={"title": title}).status_code == 422
    item = client.post("/api/items", json={"title": "Book a haircut"}).json()
    assert client.patch(f'/api/items/{item["id"]}', json={"title": title}).status_code == 422
    assert client.get("/api/items").json() == [item]


def test_missing_title_and_unsupported_fields(client):
    assert client.post("/api/items", json={"notes": "No title"}).status_code == 422
    assert client.post("/api/items", json={"title": "Task", "status": "done"}).status_code == 422
    item = client.post("/api/items", json={"title": "Book a haircut"}).json()
    assert client.patch(f'/api/items/{item["id"]}', json={"status": "done"}).status_code == 422


@pytest.mark.parametrize("method,suffix,body", [
    ("PATCH", "", {"title": "Missing"}), ("PATCH", "", {}),
    ("POST", "/complete", None), ("POST", "/reopen", None),
])
def test_missing_item_is_404(client, method, suffix, body):
    assert client.request(method, "/api/items/999" + suffix, json=body).status_code == 404


def test_items_survive_restart(cfg):
    with TestClient(create_app(cfg)) as client:
        item = client.post("/api/items", json={"title": "Email the dentist", "notes": "Confirm Thursday"}).json()
        path = f'/api/items/{item["id"]}'
        assert client.patch(path, json={"title": "Confirm the appointment"}).status_code == 200
        assert client.post(path + "/complete").status_code == 200
        client.post("/api/items", json={"title": "Finish the tax packet"})
        before = client.get("/api/items").json()
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/items").json() == before
        assert client.get("/health").json()["schema_version"] == migrate.discover()[-1].version
        assert client.post(path + "/reopen").json()["completed_at"] is None
