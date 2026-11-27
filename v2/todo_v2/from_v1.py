"""Optional read-only view of a v1 database; never an import."""
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
            # Read committed WAL rows when the sidecar is present. Without it,
            # immutable reads the main file without creating WAL/shared-memory files.
            mode = "mode=ro" if Path(str(db_path) + "-wal").exists() else "mode=ro&immutable=1"
            uri = db_path.resolve().as_uri() + "?" + mode
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                rows = conn.execute(
                    "SELECT title FROM tasks WHERE status NOT IN ('done', 'cancelled') ORDER BY id"
                ).fetchall()
        except sqlite3.Error:
            raise HTTPException(503, "The v1 snapshot could not be read") from None
        return {"enabled": True, "items": [row[0] for row in rows]}

    return api
