"""FTS5 round-trip + trigger sync tests for tool_calls_fts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from convo.read.search import SNIPPET_POST, SNIPPET_PRE, search
from tests._seed import seed_full_chain, seed_tool_call

if TYPE_CHECKING:
    from convo.db import Database


def test_search_excerpt_uses_input_json_column(db: Database) -> None:
    """Regression: snippet() for tool_call hits must render from input_json (col 1).

    `_tool_call_branch` previously passed -1 (any column) to FTS5's snippet().
    With -1, FTS5 picks whichever indexed column it considers the "best" match.
    When the search term matches the `name` column (col 0) — common when a
    user searches for a tool name like "Bash" or any token that overlaps with
    a tool identifier — FTS5 collapses the excerpt to just the bare name with
    no surrounding command context. The fix pins the snippet to col 1
    (`input_json`) so the excerpt always shows the command/argument text the
    user actually wants to see, with SNIPPET_PRE/SNIPPET_POST around any match
    that falls inside `input_json`.
    """
    # name matches the query token; input_json carries the real context the
    # user is searching for. Pre-fix the excerpt was just `<UniqueToolXyz>`;
    # post-fix it renders from input_json so the distinctive payload survives.
    distinctive = "payload_xyz"
    seed_full_chain(
        db,
        tool_name="UniqueToolXyz",
        tool_input_json=f'{{"{distinctive}": 1}}',
        tool_output_text="ok",
    )
    hits = [h for h in search(db, "UniqueToolXyz") if h.kind == "tool_call"]
    assert len(hits) == 1, f"expected one tool_call hit, got {hits}"
    hit = hits[0]
    assert hit.kind == "tool_call"
    excerpt = hit.excerpt
    # Excerpt must render from input_json (col 1), not from name (col 0).
    assert distinctive in excerpt, f"excerpt should render from input_json content, got {excerpt!r}"
    # Sentinel values are part of the snippet contract — referenced here so
    # any rename of the markers is caught at import time. The term matches
    # `name` only here, so the col-1 snippet has no marker pair to anchor.
    assert isinstance(SNIPPET_PRE, str)
    assert isinstance(SNIPPET_POST, str)


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
