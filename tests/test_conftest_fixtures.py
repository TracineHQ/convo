"""Smoke tests verifying the seeded_db_path / seeded_session_id fixtures."""

from __future__ import annotations

import sqlite3


def test_seeded_db_counts(seeded_db_path: str) -> None:
    """At least 3 sessions, 15 messages, 2 tool_calls, 2 distinct projects."""
    conn = sqlite3.connect(seeded_db_path)
    try:
        n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        n_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_tool_calls = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        n_projects = conn.execute("SELECT COUNT(DISTINCT project_path) FROM sessions").fetchone()[0]
    finally:
        conn.close()

    assert n_sessions >= 3
    assert n_messages >= 15
    assert n_tool_calls >= 2
    assert n_projects >= 2


def test_seeded_session_id_is_valid(seeded_db_path: str, seeded_session_id: str) -> None:
    """seeded_session_id resolves to an actual session in the DB."""
    conn = sqlite3.connect(seeded_db_path)
    try:
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (seeded_session_id,)).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == seeded_session_id


def test_seeded_long_session_has_60_messages(
    seeded_db_path: str, seeded_long_session_id: str
) -> None:
    """The long session has at least 60 messages."""
    conn = sqlite3.connect(seeded_db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (seeded_long_session_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert n >= 60
