"""Forward-only SQL migrations with one online backup before any of them run.

Migrations are numbered files in `migrations/` (`0001_init.sql`, `0002_...sql`). Each one
runs in its own transaction and is recorded in `schema_migrations`; `PRAGMA user_version`
mirrors the latest applied number for quick inspection. A shipped migration is never edited:
change the schema by adding the next number.

Before the first pending migration applies, the live database is copied with SQLite's online
backup API (not a file copy) to a timestamped file beside it. One run makes at most one
backup, however many migrations are pending, and none when nothing is pending.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


@dataclass(frozen=True)
class MigrationResult:
    applied: list[int]
    schema_version: int
    backup: Path | None


def discover(directory: Path | None = None) -> list[Migration]:
    """All migrations in `directory` (default: the shipped ones), in order. Gaps are an error."""
    directory = directory or MIGRATIONS_DIR
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise ValueError(f"Bad migration file name: {path.name} (want NNNN_name.sql)")
        found.append(Migration(int(match[1]), match[2], path))
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise ValueError(f"Migrations must be numbered 1..n without gaps; {migration.path.name} breaks that")
    return found


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Versions recorded in the schema table; empty for a brand-new database."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not exists:
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def schema_version(conn: sqlite3.Connection) -> int:
    return max(applied_versions(conn), default=0)


def backup(conn: sqlite3.Connection, db_path: Path, current: int) -> Path:
    """Online copy of the open database to `<db>.pre-migrate-<UTC stamp>-v<current>.bak`."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = db_path.with_name(f"{db_path.name}.pre-migrate-{stamp}-v{current}.bak")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def migrate(db_path: Path, directory: Path | None = None) -> MigrationResult:
    """Apply every pending migration to the database at `db_path` (created if missing)."""
    migrations = discover(directory)
    conn = sqlite3.connect(db_path, isolation_level=None)  # transactions are explicit below
    try:
        done = applied_versions(conn)
        unknown = done - {m.version for m in migrations}
        if unknown:
            raise RuntimeError(f"Database has migrations this build doesn't know: {sorted(unknown)}")
        pending = [m for m in migrations if m.version not in done]
        if not pending:
            return MigrationResult([], max(done, default=0), None)

        copy = backup(conn, db_path, max(done, default=0))
        for migration in pending:
            _apply(conn, migration)
        return MigrationResult([m.version for m in pending], schema_version(conn), copy)
    finally:
        conn.close()


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    # One script per migration: its SQL, its record and user_version commit together or not at all.
    # Version and name come from the validated file name, so formatting them in is safe.
    script = (
        "BEGIN;\n"
        f"{migration.path.read_text()}\n;\n"
        "INSERT INTO schema_migrations (version, name, applied_at) "
        f"VALUES ({migration.version}, '{migration.name}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
        f"PRAGMA user_version = {migration.version};\n"
        "COMMIT;"
    )
    try:
        conn.executescript(script)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
