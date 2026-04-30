"""Tests for snapshots: backup_snapshot, prune, auto, restore."""

from __future__ import annotations

import os
import re
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from convo import db as _db_module
from tests._seed import seed_source_file

if TYPE_CHECKING:
    from pathlib import Path

    from convo.db import Database


_NAME_RE = re.compile(r"^convo-\d{8}-\d{6}-\d{6}\.db$")


def _make_snap(dirpath: Path, name: str, mtime: float) -> Path:
    p = dirpath / name
    p.write_bytes(b"")
    os.utime(p, (mtime, mtime))
    return p


def test_backup_snapshot_writes_timestamped_file_and_creates_dir(
    db: Database,
    tmp_path: Path,
) -> None:
    target = tmp_path / "snaps"
    assert not target.exists()
    written = db.backup_snapshot(target)
    assert written.parent == target
    assert _NAME_RE.match(written.name), written.name
    assert target.is_dir()
    assert written.exists()


def test_backup_snapshot_honors_env_var(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_dir = tmp_path / "envsnap"
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(env_dir))
    written = db.backup_snapshot()
    assert written.parent == env_dir
    assert written.exists()


def test_backup_snapshot_falls_back_to_default(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONVO_BACKUP_DIR", raising=False)
    fake_default = tmp_path / "default"
    monkeypatch.setattr(_db_module, "DEFAULT_SNAPSHOT_DIR", fake_default)
    written = db.backup_snapshot()
    assert written.parent == fake_default


def test_prune_keeps_n_newest_and_ignores_non_glob(
    db: Database,
    tmp_path: Path,
) -> None:
    snaps = [
        _make_snap(tmp_path, f"convo-2026010{i}-000000-000000.db", 1700000000.0 + i)
        for i in range(8)
    ]
    (tmp_path / "unrelated.txt").write_text("x")
    (tmp_path / "convo-notes.md").write_text("notes")

    deleted = db.prune_snapshots(tmp_path, keep_n=3)
    assert len(deleted) == 5

    remaining = sorted(tmp_path.glob("convo-*.db"))
    assert len(remaining) == 3
    # 3 newest (highest mtime: indexes 5, 6, 7) survive.
    assert set(remaining) == set(snaps[-3:])

    assert (tmp_path / "unrelated.txt").exists()
    assert (tmp_path / "convo-notes.md").exists()


def test_prune_missing_dir_returns_empty(db: Database, tmp_path: Path) -> None:
    assert db.prune_snapshots(tmp_path / "nope") == []


def test_auto_snapshot_writes_and_prunes(
    db: Database,
    tmp_path: Path,
) -> None:
    for i in range(8):
        _make_snap(
            tmp_path,
            f"convo-2026010{i}-000000-000000.db",
            1700000000.0 + i,
        )

    written = db.auto_snapshot(tmp_path, keep_n=3)
    assert written.exists()
    assert _NAME_RE.match(written.name)

    remaining = sorted(tmp_path.glob("convo-*.db"))
    assert len(remaining) == 3
    assert written in remaining


def test_restore_snapshot_happy_path(db: Database, tmp_path: Path) -> None:
    seed_source_file(db, path="/a.jsonl")
    snap = db.backup_snapshot(tmp_path)
    seed_source_file(db, path="/b.jsonl")

    db.restore_snapshot(snap)

    assert db.conn is not None
    rows = db.conn.execute("SELECT path FROM source_files").fetchall()
    assert [r[0] for r in rows] == ["/a.jsonl"]


def test_restore_missing_source_raises(db: Database, tmp_path: Path) -> None:
    seed_source_file(db, path="/keep.jsonl")
    assert db.conn is not None
    before = db.conn.execute("SELECT count(*) FROM source_files").fetchone()[0]

    with pytest.raises(ValueError, match="does not exist"):
        db.restore_snapshot(tmp_path / "missing.db")

    after = db.conn.execute("SELECT count(*) FROM source_files").fetchone()[0]
    assert after == before


def test_restore_garbage_file_raises(db: Database, tmp_path: Path) -> None:
    seed_source_file(db, path="/keep.jsonl")
    bogus = tmp_path / "garbage.db"
    bogus.write_bytes(b"not a database")

    assert db.conn is not None
    before = db.conn.execute("SELECT count(*) FROM source_files").fetchone()[0]

    with pytest.raises(ValueError, match="not a usable convo DB"):
        db.restore_snapshot(bogus)

    after = db.conn.execute("SELECT count(*) FROM source_files").fetchone()[0]
    assert after == before


def test_restore_future_version_raises(db: Database, tmp_path: Path) -> None:
    seed_source_file(db, path="/keep.jsonl")
    # Build a snapshot with elevated user_version
    snap = db.backup_snapshot(tmp_path)
    raw = sqlite3.connect(snap)
    raw.executescript("PRAGMA user_version = 99;")
    raw.close()

    assert db.conn is not None
    before = db.conn.execute("SELECT count(*) FROM source_files").fetchone()[0]

    with pytest.raises(ValueError, match="newer schema"):
        db.restore_snapshot(snap)

    after = db.conn.execute("SELECT count(*) FROM source_files").fetchone()[0]
    assert after == before


def test_restore_unlinks_sidecars_before_replace(
    db: Database,
    tmp_path: Path,
) -> None:
    seed_source_file(db, path="/keep.jsonl")
    snap = db.backup_snapshot(tmp_path)

    wal_path = tmp_path / "convo.db-wal"
    shm_path = tmp_path / "convo.db-shm"

    observed: dict[str, bool] = {}

    real_replace = os.replace

    def spy_replace(src, dst):
        observed["wal_existed_at_replace"] = wal_path.exists()
        observed["shm_existed_at_replace"] = shm_path.exists()
        real_replace(src, dst)

    with patch.object(os, "replace", side_effect=spy_replace):
        db.restore_snapshot(snap)

    assert observed["wal_existed_at_replace"] is False
    assert observed["shm_existed_at_replace"] is False
