"""/health, startup migrations and the SPA fallback."""
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from todo_v2 import __version__, config, migrate
from todo_v2.app import create_app


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Todo</title>")
    (dist / "assets" / "app.js").write_text("console.log('v2')")
    return replace(config.load(), db_path=tmp_path / "data" / "todo-v2.db", frontend_dist=dist, commit="abc123")


def boot(cfg: config.Config) -> dict:
    with TestClient(create_app(cfg)) as client:  # the context manager runs startup (migrations)
        response = client.get("/health")
        assert response.status_code == 200
        return response.json()


def backups(cfg: config.Config) -> list[Path]:
    return sorted(cfg.db_path.parent.glob(f"{cfg.db_path.name}.pre-migrate-*.bak"))


def test_health_reports_version_and_schema(cfg: config.Config) -> None:
    body = boot(cfg)
    assert body == {
        "ok": True,
        "version": __version__,
        "commit": "abc123",
        "schema_version": migrate.discover()[-1].version,
    }


def test_restart_with_a_pending_migration_makes_exactly_one_backup(
    cfg: config.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, directory)
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", directory)

    assert boot(cfg)["schema_version"] == 1
    first = backups(cfg)
    assert boot(cfg)["schema_version"] == 1  # restart, nothing pending
    assert backups(cfg) == first

    (directory / "0002_widgets.sql").write_text("CREATE TABLE widgets (id INTEGER PRIMARY KEY);")
    assert boot(cfg)["schema_version"] == 2  # restart with one pending
    assert len(backups(cfg)) == len(first) + 1


def test_spa_serves_index_assets_and_404s_unknown_api(cfg: config.Config) -> None:
    with TestClient(create_app(cfg)) as client:
        assert "<title>Todo</title>" in client.get("/").text
        assert "<title>Todo</title>" in client.get("/some/client/route").text
        assert client.get("/assets/app.js").text == "console.log('v2')"
        assert client.get("/api/nope").status_code == 404
