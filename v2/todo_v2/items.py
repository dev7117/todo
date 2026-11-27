"""Items belong only to the configured v2 database; each request owns its connection."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CreateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title
    notes: str = ""


class EditItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title | None = None
    notes: str = ""

    @field_validator("title")
    @classmethod
    def title_cannot_be_null(cls, value):
        # Omission leaves the title alone; explicit null is not a title.
        if value is None:
            raise ValueError("Title must not be null")
        return value


class Item(BaseModel):
    id: int
    title: str
    notes: str
    status: Literal["open", "done"]
    created_at: str
    updated_at: str
    completed_at: str | None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def router(db_path: Path) -> APIRouter:
    api = APIRouter(prefix="/api/items", tags=["items"])

    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(conn, item_id: int) -> dict:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Item not found")
        return dict(row)

    @api.get("", response_model=list[Item])
    def list_items():
        with closing(connect()) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM items ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id"
            )]

    @api.post("", status_code=201, response_model=Item)
    def create_item(body: CreateItem):
        stamp = now()
        with closing(connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO items (title, notes, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (body.title, body.notes, stamp, stamp),
            )
            return get(conn, cursor.lastrowid)

    @api.patch("/{item_id}", response_model=Item)
    def edit_item(item_id: int, body: EditItem):
        changes = body.model_dump(exclude_unset=True)
        with closing(connect()) as conn, conn:
            if changes:
                # Column names come only from the validated model, never client keys.
                assignments = ", ".join(f"{key} = ?" for key in changes)
                conn.execute(
                    f"UPDATE items SET {assignments}, updated_at = ? WHERE id = ?",
                    (*changes.values(), now(), item_id),
                )
            return get(conn, item_id)

    def set_status(item_id: int, status: str):
        stamp = now()
        with closing(connect()) as conn, conn:
            # Repeating a transition is a no-op, including its timestamps.
            conn.execute(
                "UPDATE items SET status = ?, updated_at = ?, completed_at = ? "
                "WHERE id = ? AND status != ?",
                (status, stamp, stamp if status == "done" else None, item_id, status),
            )
            return get(conn, item_id)

    @api.post("/{item_id}/complete", response_model=Item)
    def complete_item(item_id: int):
        return set_status(item_id, "done")

    @api.post("/{item_id}/reopen", response_model=Item)
    def reopen_item(item_id: int):
        return set_status(item_id, "open")

    return api
