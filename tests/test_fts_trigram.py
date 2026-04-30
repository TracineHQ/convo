"""Trigram tokenizer behaviors: substring match + 2-char silent no-op."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests._seed import (
    seed_full_chain,
    seed_message,
    seed_session,
    seed_source_file,
)

if TYPE_CHECKING:
    from convo.db import Database


def test_trigram_substring_matches_all_three_fts(db: Database) -> None:
    seed_full_chain(
        db,
        message_content="prefix foobar baz suffix",
        tool_input_json='{"query": "prefix foobar baz suffix"}',
        tool_output_text="prefix foobar baz suffix",
    )

    assert db.conn is not None
    for table in ("tool_calls_fts", "tool_results_fts", "messages_fts"):
        rows = db.conn.execute(
            f"SELECT rowid FROM {table} WHERE {table} MATCH '\"oba\"'",  # noqa: S608
        ).fetchall()
        assert len(rows) == 1, f"{table} should match trigram 'oba'"


def test_trigram_two_char_query_returns_empty(db: Database) -> None:
    sfid = seed_source_file(db)
    sid = seed_session(db, sfid)
    seed_message(db, sid, content="ab")

    assert db.conn is not None
    rows = db.conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH '\"ab\"'",
    ).fetchall()
    assert rows == []
