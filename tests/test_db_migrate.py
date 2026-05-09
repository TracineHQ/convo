"""Tests for migration discovery and migrate() guard rails."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from convo.db import Database, _discover_migrations

_BASE_TABLES = {
    "messages",
    "schema_migrations",
    "sessions",
    "source_files",
    "tool_calls",
    "tool_results",
    "guard_decisions",
}
_FTS_TABLES = {
    "messages_fts",
    "tool_calls_fts",
    "tool_results_fts",
    "guard_decisions_fts",
}


def _write(p: Path, body: str = "-- noop\n") -> None:
    p.write_text(body, encoding="utf-8")


def test_discover_returns_sorted_contiguous(tmp_path: Path) -> None:
    _write(tmp_path / "0001_a.sql")
    _write(tmp_path / "0002_b.sql")
    _write(tmp_path / "0003_c.sql")
    result = _discover_migrations(pkg_root=tmp_path)
    versions = [r[0] for r in result]
    assert versions == [1, 2, 3]
    filenames = [r[1] for r in result]
    assert filenames == ["0001_a.sql", "0002_b.sql", "0003_c.sql"]


def test_discover_raises_on_gap(tmp_path: Path) -> None:
    _write(tmp_path / "0001_a.sql")
    _write(tmp_path / "0003_c.sql")
    with pytest.raises(RuntimeError, match="Non-contiguous"):
        _discover_migrations(pkg_root=tmp_path)


def test_discover_raises_on_duplicate(tmp_path: Path) -> None:
    _write(tmp_path / "0001_a.sql")
    _write(tmp_path / "0001_b.sql")
    with pytest.raises(RuntimeError, match="Duplicate migration version 1"):
        _discover_migrations(pkg_root=tmp_path)


def test_discover_ignores_non_matching_files(tmp_path: Path) -> None:
    _write(tmp_path / "0001_a.sql")
    _write(tmp_path / "README.md", "not a migration")
    _write(tmp_path / "__init__.py", "")
    result = _discover_migrations(pkg_root=tmp_path)
    assert [r[0] for r in result] == [1]


def test_open_refuses_db_from_future_version(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    raw.executescript("PRAGMA user_version = 99;")
    raw.close()

    with pytest.raises(RuntimeError) as excinfo:
        Database(db_path).open()
    msg = str(excinfo.value)
    assert "99" in msg
    assert "refusing to downgrade" in msg

    raw = sqlite3.connect(db_path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 99
    raw.close()


def test_fresh_db_has_current_user_version_and_expected_tables(db_path: Path) -> None:
    with Database(db_path) as db:
        assert db.conn is not None
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2

        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ).fetchall()
        names = {r[0] for r in rows}
        assert names >= _BASE_TABLES
        assert names >= _FTS_TABLES

        migrations = db.conn.execute(
            "SELECT version, filename, applied_at FROM schema_migrations ORDER BY version",
        ).fetchall()
        assert len(migrations) == 2
        assert migrations[0][0] == 1
        assert migrations[0][1] == "0001_init.sql"
        assert migrations[1][0] == 2
        assert migrations[1][1] == "0002_guard_decisions.sql"
        # Parses as ISO-8601:
        datetime.fromisoformat(migrations[0][2])
        datetime.fromisoformat(migrations[1][2])


def test_upgrade_v1_to_v2_preserves_data(db_path: Path) -> None:
    """A v1-only DB populated with messages should upgrade to v2 cleanly:
    0002 adds guard_decisions tables but must not touch the v1 tables."""
    # Build a v1-only DB by applying just 0001_init.sql.
    raw = sqlite3.connect(db_path)
    init_sql = (
        Path(__file__).resolve().parent.parent / "src/convo/migrations/0001_init.sql"
    ).read_text(
        encoding="utf-8",
    )
    raw.executescript("BEGIN EXCLUSIVE;\n" + init_sql)
    raw.execute(
        "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?, ?, ?)",
        (1, "0001_init.sql", datetime.now(tz=UTC).isoformat()),
    )
    raw.execute("PRAGMA user_version = 1")
    raw.execute("COMMIT")
    # Insert some real data so we can verify it survives the upgrade.
    raw.execute(
        "INSERT INTO source_files"
        "(path, kind, size, mtime_ns, last_indexed_at) "
        "VALUES (?, 'transcript', ?, ?, ?)",
        ("/tmp/keep.jsonl", 100, 0, "2026-05-08T00:00:00Z"),
    )
    raw.commit()
    raw.close()

    # Now open via Database — that triggers migrate() and applies 0002.
    with Database(db_path) as db:
        assert db.conn is not None
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2

        # 0002 added the guard_decisions tables.
        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ).fetchall()
        names = {r[0] for r in rows}
        assert "guard_decisions" in names
        assert "guard_decisions_fts" in names

        # v1 row survived.
        kept = db.conn.execute(
            "SELECT path FROM source_files WHERE path = ?",
            ("/tmp/keep.jsonl",),
        ).fetchone()
        assert kept is not None
        assert kept[0] == "/tmp/keep.jsonl"

        # Both migration rows now in schema_migrations.
        migrations = [
            (r[0], r[1])
            for r in db.conn.execute(
                "SELECT version, filename FROM schema_migrations ORDER BY version",
            )
        ]
        assert migrations == [(1, "0001_init.sql"), (2, "0002_guard_decisions.sql")]


def test_reopen_does_not_rerun_migration(db_path: Path) -> None:
    with Database(db_path) as db:
        assert db.conn is not None
        first = db.conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1",
        ).fetchone()[0]

    with Database(db_path) as db:
        assert db.conn is not None
        rows = db.conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
        assert rows[0] == 2
        second = db.conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1",
        ).fetchone()[0]
        assert first == second
