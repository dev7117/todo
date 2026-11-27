"""Ordered owner-scoped blocks; task audit and block writes share one transaction."""
from contextlib import closing
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, StrictBool, computed_field, field_validator

from .db import connect, now
from .markdown import render
from .tasks import Parent


class CreateBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["note", "subtask"]
    body: str = ""


class EditBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str | None = None
    done: StrictBool | None = None

    @field_validator("body", "done")
    @classmethod
    def cannot_be_null(cls, value):
        if value is None:
            raise ValueError("Field must not be null")
        return value


class Reorder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[Parent]


class Block(BaseModel):
    id: int
    task_id: int | None
    idea_id: int | None
    kind: Literal["note", "subtask"]
    body: str
    position: int
    done: bool
    done_at: str | None
    created_at: str
    updated_at: str

    @computed_field
    @property
    def body_html(self) -> str:
        return render(self.body)


def owner_column(owner: str) -> str:
    # Only internal constants reach SQL identifiers, never request strings.
    return {"tasks": "task_id", "ideas": "idea_id"}[owner]


def require_owner(conn, owner, owner_id):
    owner_column(owner)
    if conn.execute(f"SELECT 1 FROM {owner} WHERE id = ?", (owner_id,)).fetchone() is None:
        raise HTTPException(404, "Owner not found")


def ordered(conn, owner: str, owner_id: int) -> list[dict]:
    return [dict(row) for row in conn.execute(
        f"SELECT * FROM blocks WHERE {owner_column(owner)} = ? ORDER BY position, id", (owner_id,))]


def record_change(conn, owner, owner_id, stamp, before, after):
    conn.execute(f"UPDATE {owner} SET updated_at = ? WHERE id = ?", (stamp, owner_id))
    if owner == "tasks":
        conn.execute(
            "INSERT INTO task_history (task_id, timestamp, field, old_value, new_value) VALUES (?, ?, 'blocks', ?, ?)",
            (owner_id, stamp, json.dumps(before), json.dumps(after)),
        )


def router(db_path: Path) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["blocks"])

    def get(conn, block_id):
        row = conn.execute("SELECT * FROM blocks WHERE id = ?", (block_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Block not found")
        return dict(row)

    def list_blocks(owner, owner_id):
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN")
            require_owner(conn, owner, owner_id)
            return ordered(conn, owner, owner_id)

    def add(owner, owner_id, body):
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            require_owner(conn, owner, owner_id)
            before = ordered(conn, owner, owner_id)
            position = before[-1]["position"] + 1 if before else 0
            stamp = now()
            cursor = conn.execute(
                f"INSERT INTO blocks ({owner_column(owner)}, kind, body, position, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (owner_id, body.kind, body.body, position, stamp, stamp),
            )
            record_change(conn, owner, owner_id, stamp, before, ordered(conn, owner, owner_id))
            return get(conn, cursor.lastrowid)

    def reorder(owner, owner_id, body):
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            require_owner(conn, owner, owner_id)
            before = ordered(conn, owner, owner_id)
            ids = [row["id"] for row in before]
            if len(body.ids) != len(ids) or set(body.ids) != set(ids):
                raise HTTPException(422, "Include every block on this owner exactly once")
            if body.ids != ids:
                stamp = now()
                # Move all positions out of the destination range before filling it,
                # so the unique owner/position indexes hold throughout the reorder.
                offset = max(row["position"] for row in before) + len(ids) + 1
                conn.execute(f"UPDATE blocks SET position = position + ? WHERE {owner_column(owner)} = ?",
                             (offset, owner_id))
                for position, block_id in enumerate(body.ids):
                    conn.execute("UPDATE blocks SET position = ?, updated_at = ? WHERE id = ?",
                                 (position, stamp, block_id))
                record_change(conn, owner, owner_id, stamp, before, ordered(conn, owner, owner_id))
            return ordered(conn, owner, owner_id)

    @api.get("/tasks/{task_id}/blocks", response_model=list[Block])
    def task_blocks(task_id: int):
        return list_blocks("tasks", task_id)

    @api.get("/ideas/{idea_id}/blocks", response_model=list[Block])
    def idea_blocks(idea_id: int):
        return list_blocks("ideas", idea_id)

    @api.post("/tasks/{task_id}/blocks", status_code=201, response_model=Block)
    def add_task_block(task_id: int, body: CreateBlock):
        return add("tasks", task_id, body)

    @api.post("/ideas/{idea_id}/blocks", status_code=201, response_model=Block)
    def add_idea_block(idea_id: int, body: CreateBlock):
        return add("ideas", idea_id, body)

    @api.put("/tasks/{task_id}/blocks/order", response_model=list[Block])
    def reorder_task_blocks(task_id: int, body: Reorder):
        return reorder("tasks", task_id, body)

    @api.put("/ideas/{idea_id}/blocks/order", response_model=list[Block])
    def reorder_idea_blocks(idea_id: int, body: Reorder):
        return reorder("ideas", idea_id, body)

    @api.patch("/blocks/{block_id}", response_model=Block)
    def edit_block(block_id: int, body: EditBlock):
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            block = get(conn, block_id)
            fields = body.model_dump(exclude_unset=True)
            if "done" in fields and block["kind"] != "subtask":
                raise HTTPException(422, "Only checklist blocks can be checked")
            changes = {key: value for key, value in fields.items() if block[key] != value}
            if changes:
                owner = "tasks" if block["task_id"] is not None else "ideas"
                owner_id = block[owner_column(owner)]
                before = ordered(conn, owner, owner_id)
                stamp = now()
                if "done" in changes:
                    changes["done_at"] = stamp if changes["done"] else None
                assignments = ", ".join(f"{key} = ?" for key in changes)
                conn.execute(f"UPDATE blocks SET {assignments}, updated_at = ? WHERE id = ?",
                             (*changes.values(), stamp, block_id))
                record_change(conn, owner, owner_id, stamp, before, ordered(conn, owner, owner_id))
            return get(conn, block_id)

    @api.delete("/blocks/{block_id}", status_code=204)
    def delete_block(block_id: int):
        with closing(connect(db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            block = get(conn, block_id)
            owner = "tasks" if block["task_id"] is not None else "ideas"
            owner_id = block[owner_column(owner)]
            before = ordered(conn, owner, owner_id)
            conn.execute("DELETE FROM blocks WHERE id = ?", (block_id,))
            record_change(conn, owner, owner_id, now(), before, ordered(conn, owner, owner_id))
        return Response(status_code=204)

    return api
