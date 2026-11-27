"""Settings from the environment. Every name is TODO_V2_* so nothing can point v2 at v1's data."""
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


def load() -> Config:
    return Config(
        db_path=Path(os.getenv("TODO_V2_DB", "data/todo-v2.db")),
        frontend_dist=Path(os.getenv("TODO_V2_FRONTEND_DIST", _HERE.parent / "frontend" / "dist")),
        host=os.getenv("TODO_V2_HOST", "0.0.0.0"),
        port=int(os.getenv("TODO_V2_PORT", "7680")),
        commit=os.getenv("GIT_SHA", "dev"),
    )
