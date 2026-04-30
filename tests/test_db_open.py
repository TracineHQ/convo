"""Tests for Database.open(), close(), and context manager behavior."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from convo import db as _db_module
from convo.db import Database

if TYPE_CHECKING:
    from pathlib import Path

_BOOM = "boom"


def test_open_raises_when_sqlite_too_old(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 30, 0))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.30.0")
    monkeypatch.setattr(_db_module, "_sql", sqlite3)

    with pytest.raises(RuntimeError) as excinfo:
        Database(db_path).open()
    msg = str(excinfo.value)
    assert "3.37" in msg
    assert "3.30" in msg


def test_open_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "deeper" / "x.db"
    assert not nested.parent.exists()
    db = Database(nested)
    db.open()
    try:
        assert nested.parent.exists()
    finally:
        db.close()


def test_open_applies_pragmas_and_row_factory(db_path: Path) -> None:
    db = Database(db_path)
    db.open()
    try:
        assert db.conn is not None
        assert db.conn.row_factory is sqlite3.Row
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert db.conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert db.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert db.conn.execute("PRAGMA mmap_size").fetchone()[0] == 268435456
        assert db.conn.execute("PRAGMA cache_size").fetchone()[0] == -64000
    finally:
        db.close()


def test_close_idempotent_without_open(db_path: Path) -> None:
    db = Database(db_path)
    db.close()
    assert db.conn is None


def test_close_idempotent_after_open(db_path: Path) -> None:
    db = Database(db_path)
    db.open()
    db.close()
    db.close()
    assert db.conn is None


def _raise_boom() -> None:
    raise ValueError(_BOOM)


def test_context_manager_closes_on_exception(db_path: Path) -> None:
    db = Database(db_path)
    with pytest.raises(ValueError, match=_BOOM), db:
        _raise_boom()
    assert db.conn is None


def test_context_manager_normal_exit(db_path: Path) -> None:
    db = Database(db_path)
    with db as opened:
        assert opened is db
        assert db.conn is not None
    assert db.conn is None
