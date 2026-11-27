"""UI-facing project REST API. No MCP or agent completion interface is exposed."""
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from .db import connect, now
from .tasks import Task

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Status = Literal["active", "paused", "done", "archived"]


class CreateProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title
    description: str = ""
    status: Literal["active", "paused", "archived"] = "active"
    target_on: date | None = None

    @field_validator("target_on", mode="before")
    @classmethod
    def date_text(cls, value):
        if value is not None:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError("Use a YYYY-MM-DD date")
        return value


class EditProject(CreateProject):
    title: Title | None = None
    description: str | None = None
    status: Status | None = None

    @field_validator("title", "description", "status")
    @classmethod
    def cannot_be_null(cls, value):
        if value is None:
            raise ValueError("Field must not be null")
        return value


class Progress(BaseModel):
    done: int
    total: int
    percent: float


class Project(BaseModel):
    id: int
    title: str
    description: str
    status: Status
    target_on: str | None
    legacy_id: int | None
    created_at: str
    updated_at: str
    progress: Progress


class ProjectDetail(Project):
    tasks: list[Task]


def progress(conn, project_id: int) -> dict:
    """Count non-cancelled tasks without non-cancelled children in this project.

    Cancelled and out-of-project children do not replace their parent in this
    rollup. A parent with only cancelled children therefore counts as a leaf.
    """
    row = conn.execute(
        "SELECT count(*) AS total, coalesce(sum(status = 'done'), 0) AS done "
        "FROM tasks t WHERE project_id = ? AND status != 'cancelled' "
        "AND NOT EXISTS (SELECT 1 FROM tasks child WHERE child.parent_id = t.id "
        "AND child.project_id = t.project_id AND child.status != 'cancelled')",
        (project_id,),
    ).fetchone()
    return {"done": row["done"], "total": row["total"],
            "percent": 100 * row["done"] / row["total"] if row["total"] else 0}


def router(db_path: Path) -> APIRouter:
    api = APIRouter(prefix="/api/projects", tags=["projects"])

    def get(conn, project_id):
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")
        return {**dict(row), "progress": progress(conn, project_id)}

    @api.get("", response_model=list[Project])
    def list_projects():
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN")
            return [get(conn, row["id"]) for row in conn.execute(
                "SELECT id FROM projects ORDER BY status IN ('done', 'archived'), id"
            )]

    @api.post("", status_code=201, response_model=Project)
    def create_project(body: CreateProject):
        with closing(connect(db_path)) as conn, conn:
            stamp = now()
            cursor = conn.execute(
                "INSERT INTO projects (title, description, status, target_on, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (body.title, body.description, body.status,
                 body.target_on.isoformat() if body.target_on else None, stamp, stamp),
            )
            return get(conn, cursor.lastrowid)

    @api.get("/{project_id}", response_model=ProjectDetail)
    def get_project(project_id: int):
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN")
            project = get(conn, project_id)
            return {**project, "tasks": [dict(row) for row in conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY status IN ('done', 'cancelled'), id",
                (project_id,),
            )]}

    @api.patch("/{project_id}", response_model=Project)
    def edit_project(project_id: int, body: EditProject):
        # The SPA uses this UI-facing endpoint, including status=done. There is
        # deliberately no agent/MCP route. This is not caller authentication.
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            project = get(conn, project_id)
            changes = {key: value for key, value in body.model_dump(mode="json", exclude_unset=True).items()
                       if project[key] != value}
            if changes:
                assignments = ", ".join(f"{key} = ?" for key in changes)
                conn.execute(f"UPDATE projects SET {assignments}, updated_at = ? WHERE id = ?",
                             (*changes.values(), now(), project_id))
            return get(conn, project_id)

    return api
