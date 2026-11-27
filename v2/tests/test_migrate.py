"""The migration runner: forward-only, recorded in schema_migrations, one online backup per run."""
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from todo_v2 import migrate


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "todo-v2.db"


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    """A private copy of the shipped migrations that a test can add to."""
    directory = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, directory)
    return directory


def add_migration(directory: Path, name: str, sql: str, *, gap: int = 1) -> Path:
    version = migrate.discover(directory)[-1].version + gap
    path = directory / f"{version:04d}_{name}.sql"
    path.write_text(sql)
    return path


LATEST = migrate.discover()[-1].version


def backups(db: Path) -> list[Path]:
    return sorted(db.parent.glob(f"{db.name}.pre-migrate-*.bak"))


def tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def schema_dump(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        return rows + [("user_version", conn.execute("PRAGMA user_version").fetchone()[0], None)]


def version_of(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return migrate.schema_version(conn)


def test_fresh_db_migrates(db: Path) -> None:
    result = migrate.migrate(db)

    shipped = [m.version for m in migrate.discover()]
    assert result.applied == shipped
    assert result.schema_version == shipped[-1]
    assert "schema_migrations" in tables(db)
    assert "settings" not in tables(db)
    assert result.backup is None
    assert backups(db) == []
    with closing(sqlite3.connect(db)) as conn:
        recorded = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert recorded == shipped
        assert conn.execute("PRAGMA user_version").fetchone()[0] == shipped[-1]


def test_rerun_is_a_noop(db: Path) -> None:
    migrate.migrate(db)
    before_schema, before_backups = schema_dump(db), backups(db)

    result = migrate.migrate(db)

    assert result.applied == []
    assert result.backup is None
    assert schema_dump(db) == before_schema
    assert backups(db) == before_backups


def test_no_backup_when_nothing_is_pending(db: Path, migrations: Path) -> None:
    migrate.migrate(db, migrations)
    for old in backups(db):
        old.unlink()

    migrate.migrate(db, migrations)
    migrate.migrate(db, migrations)

    assert backups(db) == []


def test_backup_is_taken_before_the_pending_migration_applies(db: Path, migrations: Path) -> None:
    # Write the kept row into a table this test's own migration creates.
    add_migration(
        migrations,
        "widgets",
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL);",
    )
    migrate.migrate(db, migrations)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO widgets (name) VALUES ('kept')")
    already = set(backups(db))
    add_migration(migrations, "gadgets", "CREATE TABLE gadgets (id INTEGER PRIMARY KEY);")

    result = migrate.migrate(db, migrations)

    new = [b for b in backups(db) if b not in already]
    assert len(new) == 1 and result.backup == new[0]
    # The live DB has the change; the backup is the state just before it.
    assert "gadgets" in tables(db) and version_of(db) == LATEST + 2
    assert "gadgets" not in tables(new[0]) and version_of(new[0]) == LATEST + 1
    with closing(sqlite3.connect(new[0])) as copy:
        assert copy.execute("SELECT name FROM widgets").fetchone() == ("kept",)
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_several_pending_migrations_make_exactly_one_backup(db: Path, migrations: Path) -> None:
    migrate.migrate(db, migrations)
    already = set(backups(db))
    add_migration(migrations, "widgets", "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")
    add_migration(migrations, "gadgets", "CREATE TABLE gadgets (id INTEGER PRIMARY KEY);")

    result = migrate.migrate(db, migrations)

    assert result.applied == [LATEST + 1, LATEST + 2]
    new = [b for b in backups(db) if b not in already]
    assert len(new) == 1
    # Taken before either applied.
    assert version_of(new[0]) == LATEST
    assert not {"widgets", "gadgets"} & tables(new[0])


def test_first_boot_makes_no_backup(db: Path) -> None:
    assert not db.exists()

    result = migrate.migrate(db)

    assert result.applied == [m.version for m in migrate.discover()]
    assert result.backup is None
    assert db.exists()
    assert backups(db) == []


def test_fresh_db_with_several_pending_makes_no_backup(db: Path, migrations: Path) -> None:
    add_migration(migrations, "widgets", "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")
    assert not db.exists()

    result = migrate.migrate(db, migrations)

    assert result.applied == list(range(1, LATEST + 2))
    assert result.backup is None
    assert backups(db) == []
    assert "widgets" in tables(db)


def test_a_failing_migration_rolls_back_and_leaves_earlier_ones(db: Path, migrations: Path) -> None:
    migrate.migrate(db, migrations)
    add_migration(
        migrations, "broken",
        "CREATE TABLE half_done (id INTEGER PRIMARY KEY);\nINSERT INTO no_such_table VALUES (1);",
    )

    with pytest.raises(sqlite3.OperationalError):
        migrate.migrate(db, migrations)

    assert version_of(db) == LATEST
    assert "half_done" not in tables(db)


def test_numbering_gaps_are_rejected(migrations: Path) -> None:
    add_migration(migrations, "skipped", "SELECT 1;", gap=2)
    with pytest.raises(ValueError, match="without gaps"):
        migrate.discover(migrations)


def test_a_db_ahead_of_this_build_is_refused(db: Path, migrations: Path) -> None:
    added = add_migration(migrations, "widgets", "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")
    migrate.migrate(db, migrations)
    added.unlink()

    with pytest.raises(RuntimeError, match="doesn't know"):
        migrate.migrate(db, migrations)
