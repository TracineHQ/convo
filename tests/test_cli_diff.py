"""Integration tests for `convo diff` CLI command."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from convo.cli import main
from convo.db import Database
from tests._seed import seed_message, seed_source_file

if TYPE_CHECKING:
    from pathlib import Path


def _ts(offset: timedelta) -> str:
    return (datetime.now(UTC) - offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def _populate(path: Path) -> None:
    """Seed rows in both current (3d ago) and previous (10d ago) windows."""
    with Database(path) as db:
        sf_cur = seed_source_file(db, path="/data/cur.jsonl")
        sf_prev = seed_source_file(db, path="/data/prev.jsonl")
        cur_ts = _ts(timedelta(days=3))
        prev_ts = _ts(timedelta(days=10))
        cur_end = _ts(timedelta(days=3, seconds=-30))
        prev_end = _ts(timedelta(days=10, seconds=-10))
        assert db.conn is not None
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at, "
            "ended_at, model) VALUES (?, ?, ?, ?, ?, ?)",
            ("sCur", sf_cur, "/proj/X", cur_ts, cur_end, "opus-4"),
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at, "
            "ended_at, model) VALUES (?, ?, ?, ?, ?, ?)",
            ("sPrev", sf_prev, "/proj/X", prev_ts, prev_end, "sonnet-4"),
        )
        db.conn.commit()
        seed_message(db, "sCur", mid="mC", content="run build current")
        seed_message(db, "sPrev", mid="mP", content="run build previous")
        db.conn.execute("UPDATE messages SET timestamp = ? WHERE id = 'mC'", (cur_ts,))
        db.conn.execute("UPDATE messages SET timestamp = ? WHERE id = 'mP'", (prev_ts,))
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, "
            "input_json, started_at, duration_ms) "
            "VALUES ('tcC', 'mC', 'sCur', 0, 'Bash', '{}', ?, 100)",
            (cur_ts,),
        )
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, "
            "input_json, started_at, duration_ms) "
            "VALUES ('tcP', 'mP', 'sPrev', 0, 'Read', '{}', ?, 100)",
            (prev_ts,),
        )
        db.conn.commit()


def test_diff_prose_mentions_current_previous_and_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["diff"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "convo diff" in out
    assert "current" in out
    assert "previous" in out
    assert "Δ" in out
    # Both side-by-side metrics show up
    assert "tool_calls_total" in out
    assert "Bash" in out
    assert "Read" in out


def test_diff_json_envelope_has_both_windows_and_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["diff", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2  # v2 envelope
    body = payload["diff"]
    for key in ("span_seconds", "project", "current", "previous", "deltas"):
        assert key in body
    # current/previous windows are populated
    assert body["current"]["tool_calls_total"] == 1
    assert body["previous"]["tool_calls_total"] == 1
    # Deltas: tool_calls_by_name should have both Bash (+1, new) and Read (-1)
    by_name = body["deltas"]["tool_calls_by_name"]
    assert "Bash" in by_name
    assert "Read" in by_name
    assert by_name["Bash"]["absolute"] == 1
    assert by_name["Bash"]["pct"] is None
    assert by_name["Read"]["absolute"] == -1
    assert by_name["Read"]["pct"] == -1.0


def test_diff_with_since_overrides_default_span(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["diff", "--since", "30d", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    body = payload["diff"]
    # 30d window covers both seeded rows in current; previous is empty.
    assert body["span_seconds"] == timedelta(days=30).total_seconds()
    assert body["current"]["tool_calls_total"] == 2
    assert body["previous"]["tool_calls_total"] == 0


def test_diff_with_project_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["diff", "--project", "/proj/X", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["diff"]["project"] == "/proj/X"


def test_diff_empty_db_no_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    with Database(live):
        pass

    rc = main(["diff"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "convo diff" in out


def test_diff_help_lists_all_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["diff", "--help"])
    out = capsys.readouterr().out
    for flag in ("--since", "--project", "--json"):
        assert flag in out


def test_diff_resolves_project_fuzzy(seeded_db_path: str) -> None:

    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "diff",
            "--project",
            "tracine-ops",
            "--json",
        ],
        text=True,
    )
    data = json.loads(out)
    assert data["schema_version"] == 2
    assert "diff" in data


def test_diff_ambiguous_project_returns_error(seeded_db_path: str) -> None:

    proc = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "diff",
            "--project",
            "develop",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "ambiguous" in (proc.stdout + proc.stderr).lower()
