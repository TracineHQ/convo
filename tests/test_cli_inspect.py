"""Tests for `convo inspect` CLI command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from convo.cli import main
from convo.db import Database

if TYPE_CHECKING:
    from pathlib import Path


_SID = "deadbeef-1111-2222-3333-444455556666"
_LONG_CONTENT = "x" * 500  # > 200 chars, triggers truncation


def _populate(path: Path) -> None:
    """Seed: 1 session, 3 messages, 2 tool calls under the assistant message."""
    with Database(path) as db:
        assert db.conn is not None
        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (1, '/data/foo.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at, ended_at, "
            "model, git_branch) VALUES (?, 1, ?, ?, ?, ?, ?)",
            (
                _SID,
                "/work/foo",
                "2026-04-01T10:00:00Z",
                "2026-04-01T11:00:00Z",
                "claude-opus-4-7",
                "main",
            ),
        )
        msgs = [
            ("m1", "user", 0, "2026-04-01T10:00:00Z", "what does ls do?"),
            ("m2", "assistant", 1, "2026-04-01T10:00:30Z", _LONG_CONTENT),
            ("m3", "user", 2, "2026-04-01T10:01:00Z", "thanks"),
        ]
        for mid, role, seq, ts, content in msgs:
            db.conn.execute(
                "INSERT INTO messages(id, session_id, role, seq, timestamp, content, "
                "raw_json) VALUES (?, ?, ?, ?, ?, ?, '{}')",
                (mid, _SID, role, seq, ts, content),
            )
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
            "started_at) VALUES ('tc1', 'm2', ?, 0, 'Bash', "
            "'{\"command\": \"ls /tmp\"}', '2026-04-01T10:00:31Z')",
            (_SID,),
        )
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
            "started_at) VALUES ('tc2', 'm2', ?, 1, 'Read', "
            "'{\"path\": \"/tmp/foo.txt\"}', '2026-04-01T10:00:32Z')",
            (_SID,),
        )
        db.conn.commit()


def test_inspect_prose_header_and_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", _SID])
    assert rc == 0
    out = capsys.readouterr().out
    assert _SID in out
    assert "/work/foo" in out
    assert "claude-opus-4-7" in out
    assert "main" in out
    # Timeline numbering + role icons.
    assert "1. U:" in out
    assert "2. A:" in out
    assert "3. U:" in out
    # Tool-call inline lines under the assistant message.
    assert "  → Bash:" in out
    assert "  → Read:" in out
    # Default truncation: long content gets cut off with "...".
    assert "..." in out
    assert "x" * 500 not in out


def test_inspect_full_dumps_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", _SID, "--full"])
    assert rc == 0
    out = capsys.readouterr().out
    # With --full the entire 500-char content appears.
    assert "x" * 500 in out


def test_inspect_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", _SID, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == 1
    assert "inspect" in payload
    block = payload["inspect"]
    assert block["session"]["id"] == _SID
    assert block["session"]["project_path"] == "/work/foo"
    assert block["session"]["model"] == "claude-opus-4-7"
    assert block["session"]["git_branch"] == "main"
    assert len(block["messages"]) == 3

    # Default (no --full): content is truncated to 200 chars and `truncated` is True.
    assistant_msg = next(m for m in block["messages"] if m["id"] == "m2")
    assert len(assistant_msg["content"]) == 200 + len("...")
    assert assistant_msg["truncated"] is True
    assert len(assistant_msg["tool_calls"]) == 2
    assert assistant_msg["tool_calls"][0]["name"] == "Bash"
    assert assistant_msg["tool_calls"][1]["name"] == "Read"

    # Short message is not truncated.
    user_msg = next(m for m in block["messages"] if m["id"] == "m1")
    assert user_msg["truncated"] is False
    assert user_msg["content"] == "what does ls do?"


def test_inspect_full_json_no_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", _SID, "--full", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assistant_msg = next(m for m in payload["inspect"]["messages"] if m["id"] == "m2")
    assert assistant_msg["content"] == _LONG_CONTENT
    assert assistant_msg["truncated"] is False


def test_inspect_unique_prefix_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", "deadbeef", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["inspect"]["session"]["id"] == _SID


def test_inspect_no_match_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", "zzzznope"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.startswith("convo:")
    assert "no session matches zzzznope" in captured.err


def test_inspect_ambiguous_lists_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    # Two sessions sharing a prefix.
    with Database(live) as db:
        assert db.conn is not None
        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (1, '/data/a.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
        )
        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (2, '/data/b.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id) VALUES ('abcd1111-x', 1)",
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id) VALUES ('abcd2222-y', 2)",
        )
        db.conn.commit()

    rc = main(["inspect", "abcd"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "ambiguous" in captured.err
    assert "abcd1111" in captured.err
    assert "abcd2222" in captured.err


def test_inspect_help_lists_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["inspect", "--help"])
    out = capsys.readouterr().out
    assert "--full" in out
    assert "--json" in out


def test_top_level_help_lists_inspect(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "inspect" in out


def test_inspect_json_error_envelope_on_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --json, modeled errors emit a JSON error envelope on stdout."""
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", "no-such-session", "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert isinstance(payload["error"]["message"], str)
    assert payload["error"]["message"]


def test_inspect_latest_on_populated_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`convo inspect --latest` resolves to the newest started_at session."""
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", "--latest", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # _populate seeds a single session whose id is _SID; --latest must pick it.
    assert payload["inspect"]["session"]["id"] == _SID


def test_inspect_latest_empty_db_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`convo inspect --latest` on an empty DB exits 1 with `no sessions in DB`."""
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    # Bootstrap the schema but insert nothing.
    with Database(live):
        pass

    rc = main(["inspect", "--latest"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "convo: no sessions in DB" in captured.err


def test_inspect_no_target_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """`convo inspect` with neither session_id nor --latest exits 2."""
    with pytest.raises(SystemExit) as excinfo:
        main(["inspect"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "one of the arguments" in err or "required" in err
