"""Tests for `convo restore --latest`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from convo.cli import main
from convo.db import Database
from tests._seed import seed_source_file

if TYPE_CHECKING:
    from pathlib import Path


def test_restore_latest_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Backup, mutate, then `restore --latest` should drop the post-snapshot row."""
    live = tmp_path / "convo.db"
    snapshot_dir = tmp_path / "snaps"
    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snapshot_dir))

    with Database(live) as db:
        seed_source_file(db, path="/from-snap.jsonl")
        db.backup_snapshot()
        seed_source_file(db, path="/added-after.jsonl")

    rc = main(["restore", "--latest"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("restored from ")

    with Database(live) as restored:
        assert restored.conn is not None
        rows = restored.conn.execute("SELECT path FROM source_files").fetchall()
        assert [r[0] for r in rows] == ["/from-snap.jsonl"]


def test_restore_latest_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`restore --latest --json` emits a versioned envelope with the source path."""
    live = tmp_path / "convo.db"
    snapshot_dir = tmp_path / "snaps"
    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snapshot_dir))

    with Database(live) as db:
        seed_source_file(db, path="/from-snap.jsonl")
        db.backup_snapshot()

    rc = main(["restore", "--latest", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    source = payload["restore"]["source"]
    assert source.startswith(str(snapshot_dir))
    assert source.endswith(".db")


def test_restore_latest_picks_newest_of_many(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When multiple snapshots exist, `--latest` restores the newest one by filename."""
    live = tmp_path / "convo.db"
    snapshot_dir = tmp_path / "snaps"
    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snapshot_dir))

    with Database(live) as db:
        seed_source_file(db, path="/v1.jsonl")
        db.backup_snapshot()  # snapshot 1: contains v1
        seed_source_file(db, path="/v2.jsonl")
        db.backup_snapshot()  # snapshot 2: contains v1 + v2 (newest)
        seed_source_file(db, path="/v3.jsonl")
        # Live now also has v3, but newest snapshot only knows about v1+v2.

    rc = main(["restore", "--latest"])
    assert rc == 0
    capsys.readouterr()  # drain output

    with Database(live) as restored:
        assert restored.conn is not None
        rows = restored.conn.execute(
            "SELECT path FROM source_files ORDER BY path",
        ).fetchall()
        paths = [r[0] for r in rows]
        assert paths == ["/v1.jsonl", "/v2.jsonl"]


def test_restore_latest_empty_dir_clean_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snapshot_dir))

    rc = main(["restore", "--latest"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("convo: ")
    assert "no snapshots in" in err
    assert str(snapshot_dir) in err
    assert "Traceback" not in err


def test_restore_latest_missing_dir_clean_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Snapshot dir doesn't exist at all — same clean error path."""
    live = tmp_path / "convo.db"
    snapshot_dir = tmp_path / "does-not-exist"
    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snapshot_dir))

    rc = main(["restore", "--latest"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("convo: ")
    assert "no snapshots in" in err


def test_restore_requires_one_of_src_or_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CONVO_DB", str(tmp_path / "convo.db"))
    with pytest.raises(SystemExit):
        main(["restore"])
    err = capsys.readouterr().err
    # argparse-generated mutex-required error.
    assert "src" in err or "--latest" in err


def test_restore_src_and_latest_are_mutually_exclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CONVO_DB", str(tmp_path / "convo.db"))
    with pytest.raises(SystemExit):
        main(["restore", str(tmp_path / "snap.db"), "--latest"])
    err = capsys.readouterr().err
    assert "not allowed" in err or "argument" in err
