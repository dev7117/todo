"""The migration runner: forward-only, recorded in schema_migrations, one online backup per run."""
import shutil
import sqlite3
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


def add_migration(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql)


def backups(db: Path) -> list[Path]:
    return sorted(db.parent.glob(f"{db.name}.pre-migrate-*.bak"))


def tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def schema_dump(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        return rows + [("user_version", conn.execute("PRAGMA user_version").fetchone()[0], None)]
    finally:
        conn.close()


def version_of(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return migrate.schema_version(conn)
    finally:
        conn.close()


def test_fresh_db_migrates(db: Path) -> None:
    result = migrate.migrate(db)

    shipped = [m.version for m in migrate.discover()]
    assert result.applied == shipped
    assert result.schema_version == shipped[-1]
    assert {"schema_migrations", "settings"} <= tables(db)
    conn = sqlite3.connect(db)
    try:
        recorded = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert recorded == shipped
        assert conn.execute("PRAGMA user_version").fetchone()[0] == shipped[-1]
    finally:
        conn.close()


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
    migrate.migrate(db, migrations)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('kept', 'yes')")
    conn.close()
    already = set(backups(db))
    add_migration(migrations, "0002_widgets.sql", "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")

    result = migrate.migrate(db, migrations)

    new = [b for b in backups(db) if b not in already]
    assert len(new) == 1 and result.backup == new[0]
    # The live DB has the change; the backup is the state just before it.
    assert "widgets" in tables(db) and version_of(db) == 2
    assert "widgets" not in tables(new[0]) and version_of(new[0]) == 1
    with sqlite3.connect(new[0]) as copy:
        assert copy.execute("SELECT value FROM settings WHERE key = 'kept'").fetchone() == ("yes",)
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    copy.close()


def test_several_pending_migrations_make_exactly_one_backup(db: Path, migrations: Path) -> None:
    migrate.migrate(db, migrations)
    already = set(backups(db))
    add_migration(migrations, "0002_widgets.sql", "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")
    add_migration(migrations, "0003_gadgets.sql", "CREATE TABLE gadgets (id INTEGER PRIMARY KEY);")

    result = migrate.migrate(db, migrations)

    assert result.applied == [2, 3]
    new = [b for b in backups(db) if b not in already]
    assert len(new) == 1
    # Taken before either applied.
    assert version_of(new[0]) == 1
    assert not {"widgets", "gadgets"} & tables(new[0])


def test_fresh_db_with_several_pending_makes_exactly_one_backup(db: Path, migrations: Path) -> None:
    add_migration(migrations, "0002_widgets.sql", "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")

    result = migrate.migrate(db, migrations)

    assert result.applied == [1, 2]
    assert backups(db) == [result.backup]
    assert version_of(result.backup) == 0


def test_a_failing_migration_rolls_back_and_leaves_earlier_ones(db: Path, migrations: Path) -> None:
    migrate.migrate(db, migrations)
    add_migration(
        migrations, "0002_broken.sql",
        "CREATE TABLE half_done (id INTEGER PRIMARY KEY);\nINSERT INTO no_such_table VALUES (1);",
    )

    with pytest.raises(sqlite3.OperationalError):
        migrate.migrate(db, migrations)

    assert version_of(db) == 1
    assert "half_done" not in tables(db)


def test_numbering_gaps_are_rejected(migrations: Path) -> None:
    add_migration(migrations, "0003_skipped_two.sql", "SELECT 1;")
    with pytest.raises(ValueError, match="without gaps"):
        migrate.discover(migrations)


def test_a_db_ahead_of_this_build_is_refused(db: Path, migrations: Path) -> None:
    add_migration(migrations, "0002_widgets.sql", "CREATE TABLE widgets (id INTEGER PRIMARY KEY);")
    migrate.migrate(db, migrations)
    (migrations / "0002_widgets.sql").unlink()

    with pytest.raises(RuntimeError, match="doesn't know"):
        migrate.migrate(db, migrations)
