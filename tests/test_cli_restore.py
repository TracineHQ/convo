"""Tests for `convo restore`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from convo.cli import main
from convo.db import Database
from tests._seed import seed_source_file

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_restore_swaps_live_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "live.db"
    monkeypatch.setenv("CONVO_DB", str(live))

    # Build a snapshot with one row, then add a second row to live.
    with Database(live) as db:
        seed_source_file(db, path="/from-snap.jsonl")
        snap = db.backup_snapshot(tmp_path / "snaps")
        seed_source_file(db, path="/added-after.jsonl")

    assert main(["restore", str(snap)]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"restored from {snap}\n"

    with Database(live) as restored:
        assert restored.conn is not None
        rows = restored.conn.execute("SELECT path FROM source_files").fetchall()
        assert [r[0] for r in rows] == ["/from-snap.jsonl"]
