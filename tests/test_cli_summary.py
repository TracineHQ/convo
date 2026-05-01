"""Integration tests for `convo summary` CLI command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from convo.cli import main
from convo.db import Database
from tests._seed import seed_message, seed_source_file

if TYPE_CHECKING:
    from pathlib import Path


def _populate(path: Path) -> None:
    """Populate a small DB hitting all five families."""
    with Database(path) as db:
        sfid = seed_source_file(db, path="/data/x.jsonl")
        assert db.conn is not None
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at, "
            "ended_at, model) VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", sfid, "/proj/X", "2026-04-29T03:00:00Z", "2026-04-29T03:00:30Z", "opus-4"),
        )
        db.conn.commit()
        seed_message(db, "s1", mid="m1", content="run the build")
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, "
            "input_json, started_at, duration_ms) "
            "VALUES ('tc1', 'm1', 's1', 0, 'Bash', '{}', '2026-04-29T03:00:00Z', 100)",
        )
        db.conn.execute(
            "INSERT INTO tool_results(tool_call_id, is_error, output_text) VALUES ('tc1', 0, 'ok')",
        )
        db.conn.execute("UPDATE source_files SET message_count = 1 WHERE id = ?", (sfid,))
        db.conn.commit()


def test_summary_prose_mentions_all_families(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["summary"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "convo summary" in out
    assert "tools" in out
    assert "commands" in out
    assert "sessions" in out
    assert "files" in out
    assert "model" in out
    # data shows up
    assert "Bash" in out
    assert "run the build" in out
    assert "/data/x.jsonl" in out
    assert "opus-4" in out


def test_summary_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["summary", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    body = payload["summary"]
    for key in ("since", "project", "tools", "commands", "sessions", "files", "model"):
        assert key in body
    assert body["tools"]["total"] == 1
    assert body["commands"]["total"] == 1
    assert body["sessions"]["total"] == 1
    assert body["files"]["total"] == 1
    assert body["model"]["total"] == 1


def test_summary_with_since_and_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["summary", "--since", "30d", "--project", "/proj/X", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    body = payload["summary"]
    assert body["project"] == "/proj/X"
    assert body["since"] is not None


def test_summary_empty_db_no_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    with Database(live):
        pass

    rc = main(["summary"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "convo summary" in out
    assert "(no data)" in out


def test_summary_help_lists_all_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["summary", "--help"])
    out = capsys.readouterr().out
    for flag in ("--since", "--project", "--json"):
        assert flag in out
