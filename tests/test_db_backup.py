"""Tests for Database.backup() — explicit-dest VACUUM INTO."""

from __future__ import annotations

import re
import shutil
import sqlite3
from types import SimpleNamespace
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
        assert restored.conn.execute("PRAGMA user_version").fetchone()[0] == 5
        rows = restored.conn.execute(
            "SELECT path FROM source_files",
        ).fetchall()
        assert [r[0] for r in rows] == ["/seed/a.jsonl"]


def test_backup_refuses_overwrite(db: Database, tmp_path: Path) -> None:
    seed_source_file(db, path="/seed/a.jsonl")
    dest = tmp_path / "snap.db"
    db.backup(dest)
    with pytest.raises(FileExistsError, match=re.escape(str(dest))):
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


def test_backup_removes_a_partial_snapshot_when_the_copy_fails(
    db: Database,
    tmp_path: Path,
) -> None:
    # A half-written snapshot sorts newest, so `restore --latest` would pick it.
    seed_source_file(db, path="/seed/a.jsonl")
    assert db.conn is not None
    steps = {"n": 0, "stop_at": 0}

    def tick() -> int:
        steps["n"] += 1
        return 1 if steps["stop_at"] and steps["n"] >= steps["stop_at"] else 0

    db.conn.set_progress_handler(tick, 1)
    full = tmp_path / "full.db"
    db.backup(full)
    full.unlink()

    steps["stop_at"], steps["n"] = steps["n"] // 2, 0
    dest = tmp_path / "snap.db"
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        db.backup(dest)
    db.conn.set_progress_handler(None, 0)
    assert not dest.exists()


def test_backup_refuses_when_the_destination_lacks_space(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_source_file(db, path="/seed/a.jsonl")
    dest = tmp_path / "snap.db"
    seen: list[Path] = []

    def no_space(path: Path) -> SimpleNamespace:
        seen.append(path)
        return SimpleNamespace(total=1, used=1, free=0)

    monkeypatch.setattr(shutil, "disk_usage", no_space)
    with pytest.raises(RuntimeError, match="Not enough free space"):
        db.backup(dest)
    assert seen == [tmp_path]
    assert not dest.exists()
