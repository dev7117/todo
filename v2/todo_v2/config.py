"""Settings from the environment. TODO_V2_* keeps v2 storage separate; v1 is an optional read-only snapshot."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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
    github_token: str = field(default="", repr=False)
    github_repos: tuple[str, ...] = ()
    github_poll_minutes: int = 5
    github_api_base: str = "https://api.github.com"


def poll_minutes(value: str) -> int:
    try:
        return max(5, min(15, int(value)))
    except ValueError:
        return 5


def repos(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        repo for part in value.split(",")
        if re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repo := part.strip())
        and repo.split("/")[1] not in {".", ".."}
    ))


def load() -> Config:
    return Config(
        db_path=Path(os.getenv("TODO_V2_DB", "data/todo-v2.db")),
        frontend_dist=Path(os.getenv("TODO_V2_FRONTEND_DIST", _HERE.parent / "frontend" / "dist")),
        host=os.getenv("TODO_V2_HOST", "0.0.0.0"),
        port=int(os.getenv("TODO_V2_PORT", "7680")),
        commit=os.getenv("GIT_SHA", "dev"),
        github_token=os.getenv("TODO_V2_GITHUB_TOKEN", "").strip(),
        github_repos=repos(os.getenv("TODO_V2_GITHUB_REPOS", "")),
        github_poll_minutes=poll_minutes(os.getenv("TODO_V2_GITHUB_POLL_MINUTES", "5")),
        v1_db_path=Path(value) if (value := os.getenv("TODO_V2_V1_DB")) else None,
    )
