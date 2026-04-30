"""EXPLAIN QUERY PLAN tests for partial multiline indexes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests._seed import (
    seed_full_chain,
    seed_message,
    seed_session,
    seed_source_file,
    seed_tool_call,
)

if TYPE_CHECKING:
    from convo.db import Database


def test_tool_calls_multiline_index_used(db: Database) -> None:
    _, sid, mid, _ = seed_full_chain(db)
    # Add a multi-line tool_call
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO tool_calls(id, message_id, session_id, seq, name, "
        "input_json, has_newlines) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("tc-ml", mid, sid, 1, "Bash", '{"command": "a\\nb"}', 1),
    )
    seed_tool_call(
        db,
        message_id=mid,
        session_id=sid,
        tcid="tc-flat",
    )
    db.conn.commit()

    plan = db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM tool_calls WHERE name='Bash' AND has_newlines = 1",
    ).fetchall()
    plan_text = "\n".join(" ".join(str(c) for c in row) for row in plan)
    assert "idx_tool_calls_multiline" in plan_text


def test_messages_multiline_index_used(db: Database) -> None:
    sfid = seed_source_file(db)
    sid = seed_session(db, sfid)
    seed_message(db, sid, content="single-line", seq=0)
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO messages(id, session_id, role, seq, content, "
        "has_newlines, raw_json) VALUES ('m-ml', ?, 'user', 1, 'a\nb', 1, '{}')",
        (sid,),
    )
    db.conn.commit()

    plan = db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE session_id = ? AND has_newlines = 1",
        (sid,),
    ).fetchall()
    plan_text = "\n".join(" ".join(str(c) for c in row) for row in plan)
    assert "idx_messages_multiline" in plan_text
