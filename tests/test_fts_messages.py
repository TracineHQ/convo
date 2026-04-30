"""FTS5 round-trip + trigger sync tests for messages_fts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests._seed import seed_message, seed_session, seed_source_file

if TYPE_CHECKING:
    from convo.db import Database


def test_insert_round_trips(db: Database) -> None:
    sfid = seed_source_file(db)
    sid = seed_session(db, sfid)
    seed_message(db, sid, content="quick brown fox")

    assert db.conn is not None
    rows = db.conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'brown'",
    ).fetchall()
    assert len(rows) == 1


def test_update_replaces_match(db: Database) -> None:
    sfid = seed_source_file(db)
    sid = seed_session(db, sfid)
    seed_message(db, sid, content="quick brown fox")

    assert db.conn is not None
    db.conn.execute(
        "UPDATE messages SET content = ? WHERE id = ?",
        ("lazy yellow dog", "m1"),
    )
    db.conn.commit()
    new_hits = db.conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'yellow'",
    ).fetchall()
    old_hits = db.conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'brown'",
    ).fetchall()
    assert len(new_hits) == 1
    assert old_hits == []


def test_delete_removes_from_fts(db: Database) -> None:
    sfid = seed_source_file(db)
    sid = seed_session(db, sfid)
    seed_message(db, sid, content="quick brown fox")

    assert db.conn is not None
    db.conn.execute("DELETE FROM messages WHERE id = 'm1'")
    db.conn.commit()
    rows = db.conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'brown'",
    ).fetchall()
    assert rows == []
