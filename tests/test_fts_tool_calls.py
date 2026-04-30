"""FTS5 round-trip + trigger sync tests for tool_calls_fts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests._seed import seed_full_chain, seed_tool_call

if TYPE_CHECKING:
    from convo.db import Database


def test_insert_round_trips_through_trigger(db: Database) -> None:
    seed_full_chain(
        db,
        tool_input_json='{"command": "echo hello world"}',
    )
    assert db.conn is not None
    rows = db.conn.execute(
        "SELECT rowid FROM tool_calls_fts WHERE tool_calls_fts MATCH 'hello'",
    ).fetchall()
    assert len(rows) == 1

    snippet = db.conn.execute(
        "SELECT snippet(tool_calls_fts, 1, '<', '>', '...', 4) "
        "FROM tool_calls_fts WHERE tool_calls_fts MATCH 'hello'",
    ).fetchone()[0]
    assert "<hello>" in snippet


def test_update_replaces_match(db: Database) -> None:
    _, _sid, _mid, tcid = seed_full_chain(
        db,
        tool_input_json='{"command": "alpha"}',
    )
    assert db.conn is not None
    rows = db.conn.execute(
        "SELECT rowid FROM tool_calls_fts WHERE tool_calls_fts MATCH 'alpha'",
    ).fetchall()
    assert len(rows) == 1

    db.conn.execute(
        "UPDATE tool_calls SET input_json = ? WHERE id = ?",
        ('{"command": "bravo"}', tcid),
    )
    db.conn.commit()

    rows = db.conn.execute(
        "SELECT rowid FROM tool_calls_fts WHERE tool_calls_fts MATCH 'bravo'",
    ).fetchall()
    assert len(rows) == 1
    rows = db.conn.execute(
        "SELECT rowid FROM tool_calls_fts WHERE tool_calls_fts MATCH 'alpha'",
    ).fetchall()
    assert rows == []


def test_delete_removes_from_fts(db: Database) -> None:
    _, _sid, _mid, tcid = seed_full_chain(
        db,
        tool_input_json='{"command": "alpha"}',
    )
    assert db.conn is not None
    db.conn.execute("DELETE FROM tool_calls WHERE id = ?", (tcid,))
    db.conn.commit()
    rows = db.conn.execute(
        "SELECT rowid FROM tool_calls_fts WHERE tool_calls_fts MATCH 'alpha'",
    ).fetchall()
    assert rows == []


def test_name_column_indexed(db: Database) -> None:
    _, sid, mid, _ = seed_full_chain(db)
    seed_tool_call(
        db,
        message_id=mid,
        session_id=sid,
        tcid="tc2",
        name="WebFetch",
    )
    assert db.conn is not None
    rows = db.conn.execute(
        "SELECT rowid FROM tool_calls_fts WHERE tool_calls_fts MATCH 'name:WebFetch'",
    ).fetchall()
    assert len(rows) == 1
