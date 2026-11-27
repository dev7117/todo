"""Tasks and their append-only history commit atomically in the configured v2 DB."""
from contextlib import closing
import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .db import connect, now

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Status = Literal["inbox", "todo", "in_progress", "waiting", "done", "cancelled"]
Parent = Annotated[int, Field(strict=True, gt=0)]


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title
    notes: str = ""
    parent_id: Parent | None = None


class EditTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title | None = None
    notes: str | None = None
    status: Status | None = None
    parent_id: Parent | None = None
    project_id: Parent | None = None

    @field_validator("title", "notes", "status")
    @classmethod
    def cannot_be_null(cls, value):
        # Omission leaves a field alone; parent_id and project_id may explicitly be null.
        if value is None:
            raise ValueError("Field must not be null")
        return value


class Task(BaseModel):
    id: int
    title: str
    notes: str
    status: Status
    parent_id: int | None
    project_id: int | None
    created_at: str
    updated_at: str
    completed_at: str | None


class History(BaseModel):
    id: int
    task_id: int
    timestamp: str
    field: str
    old_value: str | None
    new_value: str | None


def router(db_path: Path) -> APIRouter:
    api = APIRouter(prefix="/api/tasks", tags=["tasks"])

    def get(conn, task_id: int) -> dict:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Task not found")
        return dict(row)

    def validate_parent(conn, parent_id: int | None, task_id: int | None = None):
        if parent_id is None:
            return
        get(conn, parent_id)  # A missing parent is 404, just like a missing task.
        if task_id is not None:
            ancestor = conn.execute(
                "WITH RECURSIVE ancestors(id, parent_id) AS ("
                "SELECT id, parent_id FROM tasks WHERE id = ? UNION "
                "SELECT t.id, t.parent_id FROM tasks t JOIN ancestors a ON t.id = a.parent_id) "
                "SELECT 1 FROM ancestors WHERE id = ?",
                (parent_id, task_id),
            ).fetchone()
            if ancestor:
                raise HTTPException(422, "A task cannot parent itself or one of its ancestors")

    def record(conn, task_id, stamp, field, old, new):
        conn.execute(
            "INSERT INTO task_history (task_id, timestamp, field, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
            (task_id, stamp, field, None if old is None else str(old), None if new is None else str(new)),
        )

    @api.get("", response_model=list[Task])
    def list_tasks():
        with closing(connect(db_path)) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM tasks ORDER BY status IN ('done', 'cancelled'), id"
            )]

    @api.post("", status_code=201, response_model=Task)
    def create_task(body: CreateTask):
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            validate_parent(conn, body.parent_id)
            stamp = now()
            cursor = conn.execute(
                "INSERT INTO tasks (title, notes, parent_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (body.title, body.notes, body.parent_id, stamp, stamp),
            )
            task = get(conn, cursor.lastrowid)
            record(conn, task["id"], stamp, "create", None, json.dumps(task))
            return task

    def edit(task_id: int, body: EditTask):
        with closing(connect(db_path)) as conn, conn:
            # Serialize read/validate/write so concurrent parent edits cannot form a cycle
            # and history's old values always describe the row actually being changed.
            conn.execute("BEGIN IMMEDIATE")
            task = get(conn, task_id)
            changes = {key: value for key, value in body.model_dump(exclude_unset=True).items()
                       if task[key] != value}
            if not changes:
                return task
            if "parent_id" in changes:
                validate_parent(conn, changes["parent_id"], task_id)
            if "project_id" in changes and changes["project_id"] is not None:
                if conn.execute("SELECT 1 FROM projects WHERE id = ?", (changes["project_id"],)).fetchone() is None:
                    raise HTTPException(422, "Project not found")
            stamp = now()
            for field, value in changes.items():
                record(conn, task_id, stamp, field, task[field], value)
            if "status" in changes:
                changes["completed_at"] = stamp if changes["status"] == "done" else None
            # Column names come only from the validated model and the derived timestamp.
            assignments = ", ".join(f"{key} = ?" for key in changes)
            conn.execute(
                f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",
                (*changes.values(), stamp, task_id),
            )
            return get(conn, task_id)

    @api.patch("/{task_id}", response_model=Task)
    def edit_task(task_id: int, body: EditTask):
        return edit(task_id, body)

    @api.post("/{task_id}/complete", response_model=Task)
    def complete_task(task_id: int):
        return edit(task_id, EditTask(status="done"))

    @api.post("/{task_id}/reopen", response_model=Task)
    def reopen_task(task_id: int):
        return edit(task_id, EditTask(status="todo"))

    @api.get("/{task_id}/history", response_model=list[History])
    def history(task_id: int):
        with closing(connect(db_path)) as conn:
            get(conn, task_id)
            return [dict(row) for row in conn.execute(
                "SELECT * FROM task_history WHERE task_id = ? ORDER BY id", (task_id,),
            )]

    return api
