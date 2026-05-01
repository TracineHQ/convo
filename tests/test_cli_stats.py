"""Integration tests for `convo stats <family>` CLI command."""

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
    """Populate a small DB usable across all five families."""
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
        # Set source_files.message_count for files family
        db.conn.execute("UPDATE source_files SET message_count = 1 WHERE id = ?", (sfid,))
        db.conn.commit()


def test_stats_tools_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["stats", "tools"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total" in out
    assert "Bash" in out
    assert "top by frequency" in out
    assert "error rates" in out


def test_stats_tools_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["stats", "tools", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    body = payload["stats"]
    assert body["family"] == "tools"
    assert body["total"] == 1
    assert isinstance(body["top_by_frequency"], list)
    assert body["top_by_frequency"][0]["name"] == "Bash"


def test_stats_commands_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)
    rc = main(["stats", "commands"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total" in out
    assert "run the build" in out


def test_stats_sessions_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)
    rc = main(["stats", "sessions"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total" in out
    assert "median_duration" in out
    assert "hour-of-day" in out


def test_stats_files_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)
    rc = main(["stats", "files"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total" in out
    assert "/data/x.jsonl" in out


def test_stats_model_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)
    rc = main(["stats", "model"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total" in out
    assert "opus-4" in out


def test_stats_model_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)
    rc = main(["stats", "model", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    body = payload["stats"]
    assert body["family"] == "model"
    assert body["total"] == 1
    assert body["null_count"] == 0
    assert body["by_model"][0]["model"] == "opus-4"


def test_stats_unknown_family_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    with pytest.raises(SystemExit) as exc_info:
        main(["stats", "foobar"])
    assert exc_info.value.code == 2


def test_stats_help_lists_all_families(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["stats", "--help"])
    out = capsys.readouterr().out
    for family in ("tools", "commands", "sessions", "files", "model"):
        assert family in out


def test_stats_empty_db_no_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    # Bootstrap empty DB
    with Database(live):
        pass
    for family in ("tools", "commands", "sessions", "files", "model"):
        capsys.readouterr()  # drain
        rc = main(["stats", family])
        assert rc == 0
        out = capsys.readouterr().out
        assert "(no data)" in out


def test_stats_empty_db_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    with Database(live):
        pass
    rc = main(["stats", "tools", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["stats"]["family"] == "tools"
    assert payload["stats"]["total"] == 0
