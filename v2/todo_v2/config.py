"""Settings from the environment. TODO_V2_* keeps v2 storage separate; v1 is an optional read-only snapshot."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Config:
    db_path: Path
    frontend_dist: Path
    host: str
    port: int
    commit: str
    v1_db_path: Path | None = None


def load() -> Config:
    return Config(
        db_path=Path(os.getenv("TODO_V2_DB", "data/todo-v2.db")),
        frontend_dist=Path(os.getenv("TODO_V2_FRONTEND_DIST", _HERE.parent / "frontend" / "dist")),
        host=os.getenv("TODO_V2_HOST", "0.0.0.0"),
        port=int(os.getenv("TODO_V2_PORT", "7680")),
        commit=os.getenv("GIT_SHA", "dev"),
        v1_db_path=Path(value) if (value := os.getenv("TODO_V2_V1_DB")) else None,
    )
