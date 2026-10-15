"""Shared test seed helpers for inserting schema rows.

Used by FTS, schema, and (later) backup/restore tests. Public entry points
are the `seed_*` functions; each commits before returning so callers can
freely query the rows.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
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


def seed_session(
    db: Database,
    source_file_id: int,
    *,
    sid: str = "s1",
    project_path: str | None = None,
    started_at: str | None = None,
) -> str:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at) VALUES (?, ?, ?, ?)",
        (sid, source_file_id, project_path, started_at),
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
    role: str = "user",
    timestamp: str | None = None,
) -> str:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO messages(id, session_id, parent_id, role, seq, timestamp, content, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '{}')",
        (mid, session_id, parent_id, role, seq, timestamp, content),
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


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "convo" / "migrations"


def build_db_at_version(path: Path, version: int) -> sqlite3.Connection:
    """Create a DB at schema `version` from the real migration files.

    Applies 0001..`version` the way `Database.open()` + `migrate()` did when
    that version was current (WAL, foreign_keys ON, each script inside BEGIN
    EXCLUSIVE). Returns the open connection so callers can seed rows; the
    caller closes it.
    """
    scripts = sorted(p for p in _MIGRATIONS_DIR.iterdir() if p.suffix == ".sql")[:version]
    assert len(scripts) == version, f"only {len(scripts)} migrations exist"
    raw = sqlite3.connect(path)
    raw.executescript("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;")
    now = datetime.now(tz=UTC).isoformat()
    for n, script in enumerate(scripts, start=1):
        raw.executescript("BEGIN EXCLUSIVE;\n" + script.read_text(encoding="utf-8"))
        raw.execute(
            "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?, ?, ?)",
            (n, script.name, now),
        )
        raw.execute(f"PRAGMA user_version = {n}")
        raw.execute("COMMIT")
    return raw


def seed_raw_session(
    raw: sqlite3.Connection,
    *,
    sid: str = "sess-0",
    source_file_id: int = 1,
    messages: int = 4,
) -> None:
    """Seed one session via SQL: messages chained by parent_id, tool calls and results."""
    raw.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (?, ?, 1, 1, ?)",
        (source_file_id, f"/synthetic/{sid}.jsonl", _NOW),
    )
    raw.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at, ended_at) "
        "VALUES (?, ?, '/synthetic', ?, ?)",
        (sid, source_file_id, _NOW, _NOW),
    )
    for n in range(messages):
        mid = f"{sid}-m{n}"
        raw.execute(
            "INSERT INTO messages(id, session_id, parent_id, role, seq, timestamp, content, "
            "raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')",
            (
                mid,
                sid,
                f"{sid}-m{n - 1}" if n else None,
                "assistant" if n % 2 else "user",
                n,
                _NOW,
                f"needle {sid} {n}",
            ),
        )
        if n % 2:
            tcid = f"{sid}-tc{n}"
            raw.execute(
                "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
                "started_at) VALUES (?, ?, ?, 0, 'Bash', ?, ?)",
                (tcid, mid, sid, f'{{"command": "echo {n}"}}', _NOW),
            )
            raw.execute(
                "INSERT INTO tool_results(tool_call_id, message_id, output_text) VALUES (?, ?, ?)",
                (tcid, mid, f"output {sid} {n}"),
            )
    raw.commit()
