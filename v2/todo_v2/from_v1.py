"""Optional view of a static, checkpointed v1 database file; never an import."""
from contextlib import closing
from pathlib import Path
import sqlite3

from fastapi import APIRouter, HTTPException


def router(db_path: Path | None) -> APIRouter:
    api = APIRouter()

    @api.get("/api/from-v1")
    def open_tasks():
        if db_path is None or not db_path.is_file():
            return {"enabled": False, "items": []}
        try:
            # immutable avoids locks, checkpoints, and writable WAL shared-memory files.
            # It reads only checkpointed pages: use a static snapshot, not a changing DB.
            uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                rows = conn.execute(
                    "SELECT title FROM tasks WHERE status NOT IN ('done', 'cancelled') ORDER BY id"
                ).fetchall()
        except sqlite3.Error:
            raise HTTPException(503, "The v1 snapshot could not be read") from None
        return {"enabled": True, "items": [row[0] for row in rows]}

    return api
