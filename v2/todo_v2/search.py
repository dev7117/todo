"""Literal phrase search, capped at 50 hits, linking every hit to its owner."""
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .db import connect

RESULT_LIMIT = 50


def require_fts5() -> None:
    """Probe the runtime library even when no migration is pending. Never fall back."""
    with closing(sqlite3.connect(":memory:")) as conn:
        try:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
        except sqlite3.Error as error:
            raise RuntimeError("Todo v2 requires SQLite FTS5; search is unavailable. Install an FTS5-enabled SQLite build.") from error


def literal_query(query: str) -> str:
    # One quoted phrase: punctuation and reserved words cannot become FTS operators.
    return '"' + query.strip().replace('\x00', ' ').replace('"', '""') + '"'


class Hit(BaseModel):
    kind: Literal["task", "idea", "block"]
    id: int
    title: str
    excerpt: str
    url: str


class Results(BaseModel):
    hits: list[Hit]
    limit: int = RESULT_LIMIT


def router(db_path: Path) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["search"])

    @api.get("/search", response_model=Results)
    def search(q: str = Query(default="", max_length=500)):
        if not q.strip():
            return Results(hits=[])
        # Source identifiers are constants. MATCH receives only the escaped phrase.
        with closing(connect(db_path)) as conn:
            rows = conn.execute("""
                SELECT 'task' AS kind, t.id, t.title, substr(t.notes, 1, 240) AS excerpt,
                       '/tasks/' || t.id AS url, tasks_fts.rank AS score
                FROM tasks_fts JOIN tasks t ON t.id = tasks_fts.rowid WHERE tasks_fts MATCH ?
                UNION ALL
                SELECT 'idea', i.id, i.title, substr(i.summary, 1, 240), '/ideas/' || i.id, ideas_fts.rank
                FROM ideas_fts JOIN ideas i ON i.id = ideas_fts.rowid WHERE ideas_fts MATCH ?
                UNION ALL
                SELECT 'block', b.id, coalesce(t.title, i.title), substr(b.body, 1, 240),
                       CASE WHEN b.task_id IS NOT NULL THEN '/tasks/' || b.task_id ELSE '/ideas/' || b.idea_id END,
                       blocks_fts.rank
                FROM blocks_fts JOIN blocks b ON b.id = blocks_fts.rowid
                LEFT JOIN tasks t ON t.id = b.task_id LEFT JOIN ideas i ON i.id = b.idea_id
                WHERE blocks_fts MATCH ?
                ORDER BY score, kind, id LIMIT ?
            """, (literal_query(q),) * 3 + (RESULT_LIMIT,)).fetchall()
            return Results(hits=[Hit(**dict(row)) for row in rows])

    return api
