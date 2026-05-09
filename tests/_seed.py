"""Shared test seed helpers for inserting schema rows.

Used by FTS, schema, and (later) backup/restore tests. Public entry points
are the `seed_*` functions; each commits before returning so callers can
freely query the rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from convo.db import Database


_NOW = "2026-04-29T00:00:00Z"


def seed_source_file(db: Database, *, path: str = "/data/x.jsonl") -> int:
    assert db.conn is not None
    cur = db.conn.execute(
        "INSERT INTO source_files(path, size, mtime_ns, last_indexed_at) VALUES (?, 0, 0, ?)",
        (path, _NOW),
    )
    db.conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def seed_session(db: Database, source_file_id: int, *, sid: str = "s1") -> str:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id) VALUES (?, ?)",
        (sid, source_file_id),
    )
    db.conn.commit()
    return sid


def seed_message(
    db: Database,
    session_id: str,
    *,
    mid: str = "m1",
    parent_id: str | None = None,
    content: str = "hi",
    seq: int = 0,
) -> str:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO messages(id, session_id, parent_id, role, seq, content, raw_json) "
        "VALUES (?, ?, ?, 'user', ?, ?, '{}')",
        (mid, session_id, parent_id, seq, content),
    )
    db.conn.commit()
    return mid


def seed_tool_call(
    db: Database,
    message_id: str,
    session_id: str,
    *,
    tcid: str = "tc1",
    name: str = "Bash",
    input_json: str = "{}",
) -> str:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json) "
        "VALUES (?, ?, ?, 0, ?, ?)",
        (tcid, message_id, session_id, name, input_json),
    )
    db.conn.commit()
    return tcid


def seed_tool_result(
    db: Database,
    tool_call_id: str,
    *,
    output_text: str = "ok",
) -> None:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO tool_results(tool_call_id, output_text) VALUES (?, ?)",
        (tool_call_id, output_text),
    )
    db.conn.commit()


def seed_guard_decision(
    db: Database,
    source_file_id: int,
    *,
    line_no: int = 1,
    hook_id: str = "guard.bash",
    decision: str = "deny",
    cwd: str = "/proj/A",
    timestamp: str = _NOW,
    schema_version: int = 1,
    mode: str = "enforce",
    event: str = "PreToolUse",
    tool_name: str = "Bash",
    reason: str = "test",
    session_id: str = "sess-1",
    raw_json: str = "{}",
) -> int:
    """Insert one guard_decisions row for stats_hooks / FTS / filter tests."""
    assert db.conn is not None
    cur = db.conn.execute(
        "INSERT INTO guard_decisions"
        "(source_file_id, line_no, schema_version, mode, timestamp, hook_id,"
        " event, tool_name, decision, reason, session_id, cwd, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_file_id,
            line_no,
            schema_version,
            mode,
            timestamp,
            hook_id,
            event,
            tool_name,
            decision,
            reason,
            session_id,
            cwd,
            raw_json,
        ),
    )
    db.conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def seed_full_chain(
    db: Database,
    *,
    message_content: str = "hi",
    tool_name: str = "Bash",
    tool_input_json: str = "{}",
    tool_output_text: str = "ok",
) -> tuple[int, str, str, str]:
    """Seed source_file → session → message → tool_call → tool_result.

    Returns (source_file_id, session_id, message_id, tool_call_id).
    """
    sfid = seed_source_file(db)
    sid = seed_session(db, sfid)
    mid = seed_message(db, sid, content=message_content)
    tcid = seed_tool_call(db, mid, sid, name=tool_name, input_json=tool_input_json)
    seed_tool_result(db, tcid, output_text=tool_output_text)
    return sfid, sid, mid, tcid
