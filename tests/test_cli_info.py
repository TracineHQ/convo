"""Tests for `convo info` CLI command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from convo.cli import main
from convo.db import Database
from tests._seed import seed_full_chain

if TYPE_CHECKING:
    from pathlib import Path


def _populate(path: Path) -> None:
    with Database(path) as db:
        seed_full_chain(db)
        # Set a project_path so top-projects has at least one entry.
        assert db.conn is not None
        db.conn.execute("UPDATE sessions SET project_path = ? WHERE id = 's1'", ("/proj/X",))
        db.conn.commit()


def test_info_prose_mentions_row_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["info"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "schema_version" in out
    assert "row counts:" in out
    assert "source_files" in out
    assert "sessions" in out
    assert "messages" in out
    assert "tool_calls" in out
    assert "tool_results" in out
    assert "snapshots:" in out
    assert "top projects" in out
    assert "/proj/X" in out


def test_info_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["info", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == 2  # v2 envelope
    info = payload["info"]
    assert info["db_schema_version"] == 2
    assert info["row_counts"]["source_files"] == 1
    assert info["row_counts"]["sessions"] == 1
    assert info["row_counts"]["messages"] == 1
    assert info["row_counts"]["tool_calls"] == 1
    assert info["row_counts"]["tool_results"] == 1
    assert info["last_indexed_at"] is not None
    assert info["top_projects"] == [{"project_path": "/proj/X", "session_count": 1}]
    assert info["db_size_bytes"] > 0
    assert isinstance(info["snapshot_dir_path"], str)
    assert info["snapshot_count"] == 0
    assert info["snapshot_total_bytes"] == 0


def test_info_empty_db_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    # Bootstrap empty DB.
    with Database(live):
        pass

    rc = main(["info"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(never)" in out
    assert "(no sessions)" in out


def test_info_unknown_project_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    with Database(live) as db:
        seed_full_chain(db)
        # Leave project_path as NULL (default).
    rc = main(["info"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "(unknown)" in out


def test_info_help_shows_json_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["info", "--help"])
    out = capsys.readouterr().out
    assert "--json" in out


def test_top_level_help_lists_info(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "info" in out


def test_info_json_error_emits_envelope_on_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When --json is requested and the command errors, stdout carries a JSON envelope.

    The error envelope contract: under --json, modeled errors emit
    {"schema_version": 2, "error": {"message": "..."}} on stdout so JSON
    consumers can `jq` the result. stderr stays clean to avoid double output.
    """
    # Point --db at a non-DB file so opening fails. (A merely-missing path would
    # be auto-created by Database.open, so we need an actively-bad target.)
    bad_db = tmp_path / "not-a-db.bin"
    bad_db.write_bytes(b"this is not a sqlite database")
    rc = main(["--db", str(bad_db), "info", "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 2  # v2 envelope
    assert isinstance(payload["error"]["message"], str)
    assert payload["error"]["message"]
