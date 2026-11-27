"""Ideas are project context. Status edits never create tasks or projects."""
from contextlib import closing
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

from .blocks import Block, ordered
from .db import connect, now
from .tasks import Parent, Title

Status = Literal["seed", "exploring", "parked", "promoted", "dropped"]


class CreateIdea(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title
    summary: str = ""
    status: Status = "seed"
    project_id: Parent | None = None


class EditIdea(CreateIdea):
    title: Title | None = None
    summary: str | None = None
    status: Status | None = None

    @field_validator("title", "summary", "status")
    @classmethod
    def cannot_be_null(cls, value):
        if value is None:
            raise ValueError("Field must not be null")
        return value


class Idea(BaseModel):
    id: int
    title: str
    summary: str
    status: Status
    project_id: int | None
    legacy_id: int | None
    created_at: str
    updated_at: str


class IdeaDetail(Idea):
    blocks: list[Block]


def router(db_path: Path) -> APIRouter:
    api = APIRouter(prefix="/api/ideas", tags=["ideas"])

    def get(conn, idea_id):
        row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Idea not found")
        return dict(row)

    def validate_project(conn, project_id):
        if project_id is not None and conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise HTTPException(422, "Project not found")

    @api.get("", response_model=list[Idea])
    def list_ideas(status: Status | None = None, project_id: int | None = None):
        with closing(connect(db_path)) as conn:
            clauses, params = [], []
            if status is not None:
                clauses.append("status = ?")
                params.append(status)
            if project_id is not None:
                clauses.append("project_id = ?")
                params.append(project_id)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            return [dict(row) for row in conn.execute("SELECT * FROM ideas" + where + " ORDER BY id", params)]

    @api.post("", status_code=201, response_model=Idea)
    def create_idea(body: CreateIdea):
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            validate_project(conn, body.project_id)
            stamp = now()
            cursor = conn.execute(
                "INSERT INTO ideas (title, summary, status, project_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (body.title, body.summary, body.status, body.project_id, stamp, stamp),
            )
            return get(conn, cursor.lastrowid)

    @api.get("/{idea_id}", response_model=IdeaDetail)
    def get_idea(idea_id: int):
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN")
            return {**get(conn, idea_id), "blocks": ordered(conn, "ideas", idea_id)}

    @api.patch("/{idea_id}", response_model=Idea)
    def edit_idea(idea_id: int, body: EditIdea):
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            idea = get(conn, idea_id)
            changes = {key: value for key, value in body.model_dump(exclude_unset=True).items() if idea[key] != value}
            if "project_id" in changes:
                validate_project(conn, changes["project_id"])
            if changes:
                assignments = ", ".join(f"{key} = ?" for key in changes)
                conn.execute(f"UPDATE ideas SET {assignments}, updated_at = ? WHERE id = ?",
                             (*changes.values(), now(), idea_id))
            return get(conn, idea_id)

    return api
