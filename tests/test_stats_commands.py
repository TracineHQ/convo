"""Tests for `stats_commands` analytics family."""

from __future__ import annotations

from typing import TYPE_CHECKING

from convo.analytics import stats_commands
from tests._seed import seed_session, seed_source_file

if TYPE_CHECKING:
    from convo.db import Database


def _seed_first_messages(db: Database, *, sid: str, contents: list[str]) -> None:
    """Seed a session with given message contents in order. Role=user."""
    sfid = seed_source_file(db, path=f"/data/{sid}.jsonl")
    seed_session(db, sfid, sid=sid)
    assert db.conn is not None
    for i, content in enumerate(contents):
        db.conn.execute(
            "INSERT INTO messages(id, session_id, parent_id, role, seq, content, raw_json) "
            "VALUES (?, ?, NULL, 'user', ?, ?, '{}')",
            (f"{sid}_m{i}", sid, i, content),
        )
    db.conn.commit()


def test_stats_commands_top_frequency_and_whitespace_collapse(db: Database) -> None:
    # 4 sessions; first user messages collapse: "foo bar" / "foo  bar" / "foo\tbar"
    # all become "foo bar". Plus one distinct "baz".
    _seed_first_messages(db, sid="s1", contents=["foo bar"])
    _seed_first_messages(db, sid="s2", contents=["foo  bar"])
    _seed_first_messages(db, sid="s3", contents=["foo\tbar"])
    _seed_first_messages(db, sid="s4", contents=["baz"])

    report = stats_commands(db)
    assert report.total_sessions_with_command == 4
    counts = {c.command: c.count for c in report.top_commands}
    assert counts == {"foo bar": 3, "baz": 1}
    # Order: foo bar (3) before baz (1).
    assert report.top_commands[0].command == "foo bar"


def test_stats_commands_truncates_to_80_chars(db: Database) -> None:
    long = "x" * 100
    _seed_first_messages(db, sid="sl", contents=[long])
    report = stats_commands(db)
    assert len(report.top_commands) == 1
    assert len(report.top_commands[0].command) == 80
    assert report.top_commands[0].command == "x" * 80


def test_stats_commands_uses_first_user_message_only(db: Database) -> None:
    # Session has 3 user messages; only seq=0 counts as the "command".
    _seed_first_messages(db, sid="s1", contents=["start", "middle", "end"])
    _seed_first_messages(db, sid="s2", contents=["start"])
    report = stats_commands(db)
    assert report.total_sessions_with_command == 2
    counts = {c.command: c.count for c in report.top_commands}
    assert counts == {"start": 2}


def test_stats_commands_empty_db(db: Database) -> None:
    report = stats_commands(db)
    assert report.total_sessions_with_command == 0
    assert report.top_commands == ()
