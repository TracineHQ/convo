"""FTS5 round-trip + trigger sync tests for tool_results_fts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests._seed import seed_full_chain

if TYPE_CHECKING:
    from convo.db import Database


def test_insert_round_trips(db: Database) -> None:
    seed_full_chain(db, tool_output_text="hello world")
    assert db.conn is not None
    rows = db.conn.execute(
        "SELECT rowid FROM tool_results_fts WHERE tool_results_fts MATCH 'hello'",
    ).fetchall()
    assert len(rows) == 1


def test_update_replaces_match(db: Database) -> None:
    _, _sid, _mid, tcid = seed_full_chain(db, tool_output_text="alpha")
    assert db.conn is not None
    db.conn.execute(
        "UPDATE tool_results SET output_text = ? WHERE tool_call_id = ?",
        ("bravo", tcid),
    )
    db.conn.commit()
    new_hits = db.conn.execute(
        "SELECT rowid FROM tool_results_fts WHERE tool_results_fts MATCH 'bravo'",
    ).fetchall()
    old_hits = db.conn.execute(
        "SELECT rowid FROM tool_results_fts WHERE tool_results_fts MATCH 'alpha'",
    ).fetchall()
    assert len(new_hits) == 1
    assert old_hits == []


def test_delete_removes_from_fts(db: Database) -> None:
    _, _sid, _mid, tcid = seed_full_chain(db, tool_output_text="alpha")
    assert db.conn is not None
    db.conn.execute("DELETE FROM tool_results WHERE tool_call_id = ?", (tcid,))
    db.conn.commit()
    rows = db.conn.execute(
        "SELECT rowid FROM tool_results_fts WHERE tool_results_fts MATCH 'alpha'",
    ).fetchall()
    assert rows == []
