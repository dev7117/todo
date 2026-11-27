"""Connection policy for the v2 database (the read-only v1 source is separate)."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

BUSY_TIMEOUT_MS = 5000


def connect(path: Path, *, read_only: bool = False, isolation_level: str | None = "") -> sqlite3.Connection:
    target = f"{path.resolve().as_uri()}?mode=ro" if read_only else path
    conn = sqlite3.connect(target, uri=read_only, isolation_level=isolation_level)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn
    except BaseException:
        conn.close()
        raise


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
