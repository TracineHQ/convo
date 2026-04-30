"""Tests for Database.backup() — explicit-dest VACUUM INTO."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from convo.db import Database
from tests._seed import seed_source_file

if TYPE_CHECKING:
    from pathlib import Path


def test_backup_writes_queryable_v1_db(db: Database, tmp_path: Path) -> None:
    seed_source_file(db, path="/seed/a.jsonl")
    dest = tmp_path / "snap.db"
    db.backup(dest)

    assert dest.exists()

    with Database(dest) as restored:
        assert restored.conn is not None
        assert restored.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        rows = restored.conn.execute(
            "SELECT path FROM source_files",
        ).fetchall()
        assert [r[0] for r in rows] == ["/seed/a.jsonl"]


def test_backup_refuses_overwrite(db: Database, tmp_path: Path) -> None:
    seed_source_file(db, path="/seed/a.jsonl")
    dest = tmp_path / "snap.db"
    db.backup(dest)
    with pytest.raises(FileExistsError, match=str(dest)):
        db.backup(dest)


def test_backup_creates_parent_dirs(db: Database, tmp_path: Path) -> None:
    seed_source_file(db, path="/seed/a.jsonl")
    dest = tmp_path / "nested" / "deep" / "snap.db"
    assert not dest.parent.exists()
    db.backup(dest)
    assert dest.exists()
    assert dest.parent.is_dir()


def test_backup_refuses_empty_db(db: Database, tmp_path: Path) -> None:
    dest = tmp_path / "snap.db"
    with pytest.raises(RuntimeError, match="empty convo DB"):
        db.backup(dest)
    assert not dest.exists()
