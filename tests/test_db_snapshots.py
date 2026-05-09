"""Tests for snapshots: backup_snapshot, restore."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from convo import db as _db_module
from convo.db import Database
from tests._seed import seed_source_file

if TYPE_CHECKING:
    from pathlib import Path


_NAME_RE = re.compile(r"^convo-\d{8}-\d{6}-\d{6}\.db$")


def test_backup_snapshot_writes_timestamped_file_and_creates_dir(
    db: Database,
    tmp_path: Path,
) -> None:
    seed_source_file(db, path="/seed/a.jsonl")
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
    seed_source_file(db, path="/seed/a.jsonl")
    env_dir = tmp_path / "envsnap"
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(env_dir))
    written = db.backup_snapshot()
    assert written.parent == env_dir
    assert written.exists()


def test_backup_snapshot_defaults_to_sibling_of_db(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_source_file(db, path="/seed/a.jsonl")
    monkeypatch.delenv("CONVO_BACKUP_DIR", raising=False)
    written = db.backup_snapshot()
    assert written.parent == tmp_path / _db_module.SNAPSHOT_DIR_NAME
    assert written.exists()


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


def test_restore_preserves_snapshot_file(db: Database, tmp_path: Path) -> None:
    seed_source_file(db, path="/a.jsonl")
    snap = db.backup_snapshot(tmp_path)
    snap_size_before = snap.stat().st_size

    db.restore_snapshot(snap)

    assert snap.exists(), "restore must not consume the snapshot file"
    assert snap.stat().st_size == snap_size_before


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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX permissions not enforced on Windows",
)
def test_backup_snapshot_is_owner_only(db: Database, tmp_path: Path) -> None:
    """Snapshot files must be 0o600 — they may contain prompt/response text."""

    seed_source_file(db, path="/seed/a.jsonl")
    snap = db.backup_snapshot(tmp_path)
    mode = stat.S_IMODE(snap.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX permissions not enforced on Windows",
)
def test_live_db_and_sidecars_are_owner_only(tmp_path: Path) -> None:
    """Live DB and WAL/SHM sidecars must be 0o600. Prompts/responses live there too."""

    db_path = tmp_path / "convo.db"
    with Database(db_path) as db:
        # Force WAL sidecars by writing.
        seed_source_file(db, path="/seed/a.jsonl")
        assert db.conn is not None
        db.conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        for path in (
            db_path,
            db_path.with_name(db_path.name + "-wal"),
            db_path.with_name(db_path.name + "-shm"),
        ):
            if not path.exists():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600, f"{path.name}: expected 0o600, got 0o{mode:o}"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX permissions not enforced on Windows",
)
def test_restore_leaves_db_owner_only(db: Database, tmp_path: Path) -> None:
    """Restored live DB must be 0o600, not the staging file's umask."""

    seed_source_file(db, path="/seed/a.jsonl")
    snap = db.backup_snapshot(tmp_path)
    db.restore_snapshot(snap)
    mode = stat.S_IMODE(db.path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"
