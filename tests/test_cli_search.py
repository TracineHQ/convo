"""Tests for `convo search` CLI command."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from convo.cli import main
from convo.db import Database

if TYPE_CHECKING:
    from pathlib import Path


def _ts(offset: timedelta) -> str:
    return (datetime.now(UTC) - offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def _populate(path: Path) -> None:
    """Seed two projects with discoverable terms across messages and tool calls."""
    with Database(path) as db:
        assert db.conn is not None
        ts_now = _ts(timedelta(seconds=0))
        ts_2d = _ts(timedelta(days=2))
        ts_30d = _ts(timedelta(days=30))

        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (1, '/data/foo.jsonl', 0, 0, ?)",
            (ts_now,),
        )
        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (2, '/data/bar.jsonl', 0, 0, ?)",
            (ts_now,),
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path) VALUES ('s1', 1, '/work/foo')",
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path) VALUES ('s2', 2, '/work/bar')",
        )
        msgs = [
            ("m1", "s1", 0, ts_now, "kafka pipeline notes ingestion"),
            ("m2", "s1", 1, ts_2d, "kafka cluster planning summary"),
            ("m3", "s2", 0, ts_30d, "kafka legacy archive notes"),
            ("m4", "s2", 1, ts_now, "python analytics workflow"),
        ]
        for mid, sid, seq, ts, content in msgs:
            db.conn.execute(
                "INSERT INTO messages(id, session_id, role, seq, timestamp, content, "
                "raw_json) VALUES (?, ?, 'user', ?, ?, ?, '{}')",
                (mid, sid, seq, ts, content),
            )
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
            "started_at) VALUES ('tc1', 'm1', 's1', 0, 'Bash', "
            '\'{"command": "echo kafka started"}\', ?)',
            (ts_now,),
        )
        db.conn.execute(
            "INSERT INTO tool_results(tool_call_id, message_id, output_text) "
            "VALUES ('tc1', 'm1', 'kafka consumer ready')",
        )
        db.conn.commit()


def test_search_prose_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["search", "kafka"])
    assert rc == 0
    out = capsys.readouterr().out
    # Prose lines look like: "[kind] <ts> | <excerpt> | <session_id>"
    assert "kafka" in out.lower()
    assert "s1" in out  # at least one session id appears


def test_search_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["search", "kafka", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    # v2: envelope shape updated — filters gained session/tool_exact, hits gained indices
    assert payload["schema_version"] == 2  # v2 envelope
    assert "search" in payload
    block = payload["search"]
    assert block["query"] == "kafka"
    filters = block["filters"]
    assert filters["since"] is None
    assert filters["project"] is None
    assert filters["tool"] is None
    assert filters["limit"] == 10  # v2: default changed from 50 to 10
    assert isinstance(block["hits"], list)
    assert block["hits"], "expected at least one hit for 'kafka'"
    for hit in block["hits"]:
        required = {"kind", "id", "session_id", "timestamp", "excerpt", "indices"}
        assert required.issubset(hit.keys())
        assert isinstance(hit["indices"], list)
        # Snippet markers should be stripped from JSON output.
        assert "\x02HIT\x02" not in hit["excerpt"]
        assert "\x03HIT\x03" not in hit["excerpt"]


def test_search_limit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["search", "kafka", "--limit", "1", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["search"]["hits"]) == 1
    assert payload["search"]["filters"]["limit"] == 1


def test_search_project_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["search", "kafka", "--project", "/work/foo", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    hits = payload["search"]["hits"]
    assert hits
    assert all(h["project"] == "/work/foo" for h in hits)


def test_search_tool_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["search", "kafka", "--tool", "Bash", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    hits = payload["search"]["hits"]
    assert hits
    for h in hits:
        assert h["kind"] in {"tool_call", "tool_result"}


def test_search_since_filter_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["search", "kafka", "--since", "1d", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # v2: _span_to_str now emits human-readable spans ("1d") matching _WIDEN_TABLE
    assert payload["search"]["filters"]["since"] == "1d"


def test_search_empty_query_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["search", ""])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.startswith("convo:")


def test_search_empty_query_json_stdout_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --json, modeled errors emit a JSON error envelope on stdout."""
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["search", "", "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 2  # v2 envelope
    assert "error" in payload
    assert isinstance(payload["error"]["message"], str)
    assert payload["error"]["message"]


def test_search_invalid_since_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    # parse_span rejects "7days" — argparse turns that into a non-zero exit (2).
    with pytest.raises(SystemExit):
        main(["search", "kafka", "--since", "7days"])
    err = capsys.readouterr().err
    assert "invalid --since span" in err or "argument --since" in err


def test_search_help_lists_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["search", "--help"])
    out = capsys.readouterr().out
    assert "--since" in out
    assert "--project" in out
    assert "--tool" in out
    assert "--limit" in out
    assert "--json" in out


def test_top_level_help_lists_search(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "search" in out


def test_search_no_hits_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["search", "nonexistentterm", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["search"]["hits"] == []


def test_search_no_hits_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["search", "nonexistentterm"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "0 hits" in out  # v2: prose output reworded from "(no hits)" to "0 hits."


def test_search_limit_negative_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """`--limit -5` exits 2 with a clear argparse error.

    Regression: a negative limit used to be silently accepted (the SQL LIMIT
    clause ignored it and returned the full set), inconsistent with `--limit 0`
    which returned no hits. Both are now rejected at parse time.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(["search", "foo", "--limit", "-5"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--limit must be a positive integer" in err


def test_search_limit_zero_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """`--limit 0` exits 2: zero is a nonsense limit, treat the same as negatives."""
    with pytest.raises(SystemExit) as excinfo:
        main(["search", "foo", "--limit", "0"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--limit must be a positive integer" in err
