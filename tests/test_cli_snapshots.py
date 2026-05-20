"""Tests for `convo snapshots` CLI command."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from convo.cli import main

if TYPE_CHECKING:
    from pathlib import Path


def _write_snapshot(snapshot_dir: Path, *, when: datetime, size: int = 10) -> Path:
    name = when.strftime("convo-%Y%m%d-%H%M%S-%f.db")
    path = snapshot_dir / name
    path.write_bytes(b"x" * size)
    return path


def test_snapshots_prose_lists_files_newest_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snapshot_dir))

    now = datetime.now(UTC)
    older = _write_snapshot(snapshot_dir, when=now - timedelta(days=2), size=100)
    newer = _write_snapshot(snapshot_dir, when=now - timedelta(minutes=10), size=200)

    rc = main(["snapshots"])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(snapshot_dir) in out
    assert older.name in out
    assert newer.name in out
    # Newer file appears before older one in prose output.
    assert out.index(newer.name) < out.index(older.name)
    assert "name" in out
    assert "size" in out
    assert "age" in out


def test_snapshots_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snapshot_dir))

    now = datetime.now(UTC)
    _write_snapshot(snapshot_dir, when=now - timedelta(hours=1), size=42)

    rc = main(["snapshots", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == 2  # v2 envelope
    body = payload["snapshots"]
    assert body["snapshot_dir"] == str(snapshot_dir)
    assert len(body["entries"]) == 1
    item = body["entries"][0]
    assert set(item.keys()) == {"name", "path", "timestamp_utc", "size_bytes", "age_human"}
    assert item["size_bytes"] == 42
    assert item["name"].startswith("convo-")
    assert item["name"].endswith(".db")
    assert item["age_human"].endswith("ago")


def test_snapshots_empty_dir_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snapshot_dir))

    rc = main(["snapshots"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(no snapshots)" in out


def test_snapshots_empty_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snapshot_dir))

    rc = main(["snapshots", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshots"]["entries"] == []


def test_snapshots_help_shows_json(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["snapshots", "--help"])
    out = capsys.readouterr().out
    assert "--json" in out


def test_top_level_help_lists_snapshots(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "snapshots" in out
