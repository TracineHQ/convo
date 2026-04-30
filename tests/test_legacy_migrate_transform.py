"""Tests for per-table transform functions in legacy_migrate."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

from convo.legacy_migrate import (
    _CONVO_LEGACY_NS,
    _TOOL_RESULTS_NOOP_NOTE,
    _resolve_message_id_for_tool_call,
    _synth_tool_call_id,
    migrate_messages,
    migrate_sessions,
    migrate_source_files,
    migrate_tool_calls,
)

if TYPE_CHECKING:
    import sqlite3


def test_migrate_source_files_yields_full_columns(
    legacy_conn: sqlite3.Connection,
) -> None:
    rows = list(migrate_source_files(legacy_conn))
    assert len(rows) == 5
    paths = {r[0] for r in rows}
    assert paths == {
        "/p1/a.jsonl",
        "/p1/b.jsonl",
        "/p2/c.jsonl",
        "/p2/d.jsonl",
        "/p3/e.jsonl",
    }
    for row in rows:
        _path, kind, sha256, size, mtime_ns, indexed_at, message_count = row
        assert kind == "transcript"
        assert sha256 is None
        assert isinstance(size, int)
        assert isinstance(mtime_ns, int)
        assert message_count == 0
        assert indexed_at.startswith("2026-04-01")


def test_migrate_sessions_drops_orphans(
    legacy_conn: sqlite3.Connection,
) -> None:
    # Build a path map missing /orphan/missing.jsonl (conv-C's path).
    path_to_id = {
        "/p1/a.jsonl": 1,
        "/p1/b.jsonl": 2,
        "/p2/c.jsonl": 3,
        "/p2/d.jsonl": 4,
        "/p3/e.jsonl": 5,
    }
    results = list(migrate_sessions(legacy_conn, path_to_id))
    drops = sum(d for _, d in results)
    rows = [r for r, d in results if d == 0]
    assert drops == 1
    assert len(rows) == 2

    by_id = {r[0]: r for r in rows}
    assert "conv-A" in by_id
    assert "conv-B" in by_id
    # conv-A: source_file_id=1 (p1/a.jsonl), cwd=/work/p1
    a_row = by_id["conv-A"]
    assert a_row[1] == 1
    assert a_row[2] == "/work/p1"
    assert a_row[6] == "main"
    assert a_row[7] is None  # git_commit absent in legacy


def test_migrate_messages_synthesizes_raw_json(
    legacy_conn: sqlite3.Connection,
) -> None:
    results = list(migrate_messages(legacy_conn))
    drops = sum(d for _, d in results)
    rows = [r for r, d in results if d == 0]
    assert drops == 1  # the bad-role row
    # 5 valid rows: messages 1-5 have valid roles.
    assert len(rows) == 5

    # Inspect message id=1 (user, content_length=42)
    by_id = {r[0]: r for r in rows}
    m1 = by_id["legacy:1"]
    raw = json.loads(m1[8])
    assert raw["_synthesized"] is True
    assert raw["_legacy_id"] == 1
    assert raw["role"] == "user"
    assert raw["_legacy_content_length"] == 42
    assert m1[3] == "user"
    assert m1[6] == ""  # content placeholder
    assert m1[7] == 0  # has_newlines


def test_synth_tool_call_id_stable_and_unique() -> None:
    a = _synth_tool_call_id("conv-A", 1)
    b = _synth_tool_call_id("conv-A", 1)
    assert a == b
    # Round-trip as UUID
    assert uuid.UUID(a)
    # Different inputs differ
    assert _synth_tool_call_id("conv-A", 1) != _synth_tool_call_id("conv-A", 2)
    assert _synth_tool_call_id("conv-A", 1) != _synth_tool_call_id("conv-B", 1)
    # Match uuid5 contract
    assert a == str(uuid.uuid5(_CONVO_LEGACY_NS, "conv-A:1"))


def test_resolve_message_id_for_tool_call_lowest_id_wins(
    legacy_conn: sqlite3.Connection,
) -> None:
    # msg 2 and 3 share timestamp; lowest id (2) wins.
    resolved = _resolve_message_id_for_tool_call(
        legacy_conn,
        "conv-A",
        "2026-04-01T00:10:02Z",
    )
    assert resolved == 2


def test_resolve_message_id_returns_none_when_no_match(
    legacy_conn: sqlite3.Connection,
) -> None:
    # Timestamp doesn't match any message
    assert (
        _resolve_message_id_for_tool_call(
            legacy_conn,
            "conv-A",
            "9999-01-01T00:00:00Z",
        )
        is None
    )
    # Timestamp is None
    assert _resolve_message_id_for_tool_call(legacy_conn, "conv-A", None) is None


def test_resolve_message_id_filters_to_assistant_only(
    legacy_conn: sqlite3.Connection,
) -> None:
    # msg 1 timestamp is on a `user` row only
    assert (
        _resolve_message_id_for_tool_call(
            legacy_conn,
            "conv-A",
            "2026-04-01T00:10:01Z",
        )
        is None
    )


def test_migrate_tool_calls_drops_unresolvable_and_defaults_input_json(
    legacy_conn: sqlite3.Connection,
) -> None:
    results = list(migrate_tool_calls(legacy_conn))
    drops = sum(d for _, d in results)
    rows = [r for r, d in results if d == 0]
    # tc1 + tc2 resolve; tc3 doesn't (drop).
    assert drops == 1
    assert len(rows) == 2

    by_seq = {r[3]: r for r in rows}
    tc1 = by_seq[10]
    tc2 = by_seq[11]

    # Synthesized id is a valid UUID
    uuid.UUID(tc1[0])
    uuid.UUID(tc2[0])
    # message_id for tc1 = "legacy:2" (lowest of msg 2, 3)
    assert tc1[1] == "legacy:2"
    assert tc1[2] == "conv-A"  # session_id
    assert tc1[4] == "Bash"
    assert tc1[5] == '{"command":"echo hi"}'
    assert tc1[6] is None  # started_at
    assert tc1[7] is None  # ended_at
    assert tc1[8] is None  # duration_ms
    assert tc1[9] == 0  # has_newlines

    # tc2: NULL input_json substituted
    assert tc2[5] == "{}"
    assert tc2[9] == 1  # has_newlines from legacy


def test_boolean_to_integer_cast_is_noop(
    legacy_conn: sqlite3.Connection,
) -> None:
    # has_newlines and is_subagent come back as int from sqlite3.Row.
    results = list(migrate_tool_calls(legacy_conn))
    for row, drop in results:
        if drop:
            continue
        assert isinstance(row[9], int)


def test_tool_results_noop_note_present() -> None:
    assert "tool_results: 0 -> 0" in _TOOL_RESULTS_NOOP_NOTE
    assert "intake plan will populate" in _TOOL_RESULTS_NOOP_NOTE
