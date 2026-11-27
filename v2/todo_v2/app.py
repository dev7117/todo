"""The v2 web app: /health, /api, and the built React UI."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import connect

from . import __version__, config, from_v1, tasks, migrate
from .agent_work import AgentWork


def create_app(cfg: config.Config | None = None, *, agent_work: AgentWork | None = None) -> FastAPI:
    cfg = cfg or config.load()
    agents = agent_work or AgentWork(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        migrate.migrate(cfg.db_path)  # backs up first when anything is pending
        agents.start()
        try:
            yield
        finally:
            agents.close()

    app = FastAPI(title="Todo v2", version=__version__, lifespan=lifespan)

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            conn = connect(cfg.db_path, read_only=True)
            try:
                schema = migrate.schema_version(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            return JSONResponse(status_code=503, content={
                "ok": False, "version": __version__, "commit": cfg.commit,
                "schema_version": None, "github": agents.health(),
            })
        return JSONResponse(
            {"ok": True, "version": __version__, "commit": cfg.commit, "schema_version": schema, "github": agents.health()}
        )

    @app.get("/api/agent-work")
    def agent_work_payload() -> dict:
        return agents.payload()

    @app.post("/api/agent-work/refresh")
    def agent_work_refresh() -> dict:
        agents.refresh()
        return agents.payload()

    app.include_router(tasks.router(cfg.db_path))
    app.include_router(from_v1.router(cfg.v1_db_path))
    _mount_frontend(app, cfg.frontend_dist)
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        file = (dist / full_path).resolve()
        if full_path and file.is_file() and dist.resolve() in file.parents:
            return FileResponse(file)
        index = dist / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse(
            status_code=503, content={"detail": "Frontend not built (cd v2/frontend && npm run build)"}
        )
