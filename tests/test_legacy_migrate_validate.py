"""Tests for the validation pass: counts, samples, FTS probes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from convo.db import Database
from convo.legacy_migrate import (
    ValidationError,
    _fts5_quoted,
    _stable_sample_indices,
    _synth_tool_call_id,
    migrate_messages,
    migrate_sessions,
    migrate_source_files,
    migrate_tool_calls,
    validate,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _migrate_into(
    legacy_conn: sqlite3.Connection,
    new_db_path: Path,
) -> tuple[Database, dict[str, int]]:
    """Run the Phase 02 transforms into a fresh new-schema DB at `new_db_path`.

    Returns (open Database, dropped_per_table dict).
    """
    new = Database(new_db_path)
    new.open()
    assert new.conn is not None

    # source_files first
    new.conn.executemany(
        "INSERT INTO source_files(path, kind, sha256, size, mtime_ns, "
        "last_indexed_at, message_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        list(migrate_source_files(legacy_conn)),
    )
    path_to_id = {r[1]: r[0] for r in new.conn.execute("SELECT id, path FROM source_files")}

    drops: dict[str, int] = {
        "sessions": 0,
        "messages": 0,
        "tool_calls": 0,
    }
    sessions_rows = []
    for row, drop in migrate_sessions(legacy_conn, path_to_id):
        if drop:
            drops["sessions"] += drop
        else:
            sessions_rows.append(row)
    new.conn.executemany(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at, "
        "ended_at, model, git_branch, git_commit) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        sessions_rows,
    )

    msg_rows = []
    valid_session_ids = {r[0] for r in sessions_rows}
    for row, drop in migrate_messages(legacy_conn):
        if drop:
            drops["messages"] += drop
            continue
        # Drop messages whose session was orphaned (cascade safety).
        if row[1] not in valid_session_ids:
            drops["messages"] += 1
            continue
        msg_rows.append(row)
    new.conn.executemany(
        "INSERT INTO messages(id, session_id, parent_id, role, seq, "
        "timestamp, content, has_newlines, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        msg_rows,
    )

    tc_rows = []
    valid_msg_ids = {r[0] for r in msg_rows}
    for row, drop in migrate_tool_calls(legacy_conn):
        if drop:
            drops["tool_calls"] += drop
            continue
        if row[1] not in valid_msg_ids:
            drops["tool_calls"] += 1
            continue
        tc_rows.append(row)
    new.conn.executemany(
        "INSERT INTO tool_calls(id, message_id, session_id, seq, name, "
        "input_json, started_at, ended_at, duration_ms, has_newlines) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tc_rows,
    )
    new.conn.commit()
    return new, drops


def test_stable_sample_indices_reproducible() -> None:
    a = _stable_sample_indices(100, 5, 0xC0FFEE)
    b = _stable_sample_indices(100, 5, 0xC0FFEE)
    assert a == b
    assert len(a) == 5
    assert all(0 <= i < 100 for i in a)


def test_stable_sample_indices_clamps_k() -> None:
    out = _stable_sample_indices(3, 10, 42)
    assert len(out) == 3
    assert set(out) == {0, 1, 2}


def test_stable_sample_indices_empty() -> None:
    assert _stable_sample_indices(0, 5, 1) == []


def test_fts5_quoted_doubles_quotes() -> None:
    assert _fts5_quoted("foo") == '"foo"'
    assert _fts5_quoted('a"b') == '"a""b"'
    assert _fts5_quoted('"') == '""""'


def test_validate_counts_pass(
    legacy_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    new, drops = _migrate_into(legacy_conn, tmp_path / "new.db")
    try:
        report = validate(legacy_conn, new, dropped_per_table=drops)
        assert report.counts_passed is True
        # source_files: 5 legacy → 5 new, 0 dropped
        assert report.counts_detail["source_files"] == (5, 5, 0)
        # sessions: 3 legacy → 2 new (conv-C orphan), 1 dropped
        assert report.counts_detail["sessions"] == (3, 2, 1)
    finally:
        new.close()


def test_validate_counts_fail_on_extra_row(
    legacy_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    new, drops = _migrate_into(legacy_conn, tmp_path / "new.db")
    try:
        # Sneak in an extra source_files row to break the count
        assert new.conn is not None
        new.conn.execute(
            "INSERT INTO source_files(path, size, mtime_ns, last_indexed_at) "
            "VALUES ('/extra.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
        )
        new.conn.commit()
        with pytest.raises(ValidationError, match="source_files"):
            validate(legacy_conn, new, dropped_per_table=drops)
    finally:
        new.close()


def test_validate_samples_and_fts_probes_pass(
    legacy_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    new, drops = _migrate_into(legacy_conn, tmp_path / "new.db")
    try:
        report = validate(legacy_conn, new, dropped_per_table=drops)
        assert report.samples_passed > 0
        assert report.samples_failed == 0
        # fixture has 1 input_json that's >= 8 chars (tc1: '{"command":"echo hi"}')
        # tc3 also has '{"pattern":"foo"}', but tc3 is unresolvable.
        # The probe runs on legacy rows regardless of resolution.
        assert report.fts_probes_passed >= 0
        assert report.fts_skipped_reason is not None
        assert "messages_fts probes skipped" in report.fts_skipped_reason
    finally:
        new.close()


def test_validate_samples_fail_on_mutated_input_json(
    legacy_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    new, drops = _migrate_into(legacy_conn, tmp_path / "new.db")
    try:
        assert new.conn is not None
        # Mutate every tool_call's input_json to break sample equality
        new.conn.execute(
            "UPDATE tool_calls SET input_json = '{\"tampered\":true}'",
        )
        new.conn.commit()
        with pytest.raises(ValidationError, match="tool_calls"):
            validate(legacy_conn, new, dropped_per_table=drops)
    finally:
        new.close()


def test_validate_fts_fail_on_dropped_trigger(
    legacy_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    new, drops = _migrate_into(legacy_conn, tmp_path / "new.db")
    try:
        assert new.conn is not None
        # Wipe the FTS index to force probe miss
        new.conn.execute(
            "INSERT INTO tool_calls_fts(tool_calls_fts) VALUES('delete-all')",
        )
        new.conn.commit()
        with pytest.raises(ValidationError, match="FTS round-trip miss"):
            validate(legacy_conn, new, dropped_per_table=drops)
    finally:
        new.close()


def test_synth_id_round_trip_via_validate(
    legacy_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    new, _drops = _migrate_into(legacy_conn, tmp_path / "new.db")
    try:
        # Synthesized id is recoverable by re-running the helper
        assert new.conn is not None
        synth = _synth_tool_call_id("conv-A", 1)
        row = new.conn.execute(
            "SELECT name FROM tool_calls WHERE id = ?",
            (synth,),
        ).fetchone()
        assert row is not None
        assert row[0] == "Bash"
    finally:
        new.close()
