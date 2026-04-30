"""Tests for migration discovery and migrate() guard rails."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING

import pytest

from convo.db import Database, _discover_migrations

if TYPE_CHECKING:
    from pathlib import Path

_BASE_TABLES = {
    "messages",
    "schema_migrations",
    "sessions",
    "source_files",
    "tool_calls",
    "tool_results",
}
_FTS_TABLES = {"messages_fts", "tool_calls_fts", "tool_results_fts"}


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


def test_open_detects_legacy_convo_db(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    raw.executescript(
        "CREATE TABLE conversations (id INTEGER PRIMARY KEY, project_path TEXT);"
        "PRAGMA user_version = 2;",
    )
    raw.close()

    with pytest.raises(RuntimeError) as excinfo:
        Database(db_path).open()
    msg = str(excinfo.value)
    assert "legacy convo DB" in msg
    assert "convo migrate-legacy" in msg


def test_open_does_not_flag_v1_db_as_legacy(db_path: Path) -> None:
    # v1 convo DB has schema_migrations; should open without legacy error.
    db = Database(db_path)
    db.open()
    db.close()
    # Re-open: still fine, no legacy false-positive.
    db.open()
    db.close()


def test_open_does_not_flag_empty_db_as_legacy(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    raw.close()
    db = Database(db_path)
    db.open()
    db.close()


def test_fresh_db_has_user_version_1_and_expected_tables(db_path: Path) -> None:
    with Database(db_path) as db:
        assert db.conn is not None
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1

        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ).fetchall()
        names = {r[0] for r in rows}
        assert names >= _BASE_TABLES
        assert names >= _FTS_TABLES

        migrations = db.conn.execute(
            "SELECT version, filename, applied_at FROM schema_migrations",
        ).fetchall()
        assert len(migrations) == 1
        assert migrations[0][0] == 1
        assert migrations[0][1] == "0001_init.sql"
        # Parses as ISO-8601:
        datetime.fromisoformat(migrations[0][2])


def test_reopen_does_not_rerun_migration(db_path: Path) -> None:
    with Database(db_path) as db:
        assert db.conn is not None
        first = db.conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1",
        ).fetchone()[0]

    with Database(db_path) as db:
        assert db.conn is not None
        rows = db.conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
        assert rows[0] == 1
        second = db.conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1",
        ).fetchone()[0]
        assert first == second
