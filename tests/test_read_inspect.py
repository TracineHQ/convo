"""Tests for `inspect_session` and `resolve_session_id` in `convo.read.inspect`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from convo.read.inspect import (
    MessageView,
    SessionView,
    ToolCallView,
    inspect_session,
    resolve_session_id,
)

if TYPE_CHECKING:
    from convo.db import Database


def _seed_session(
    db: Database,
    *,
    sid: str = "deadbeef-1111-2222-3333-444455556666",
    project: str = "/work/foo",
) -> str:
    """Seed a single session with full header metadata."""
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (1, '/data/foo.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
    )
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at, ended_at, "
        "model, git_branch, git_commit) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            project,
            "2026-04-01T10:00:00Z",
            "2026-04-01T11:30:00Z",
            "claude-opus-4-7",
            "main",
            "abc123",
        ),
    )
    db.conn.commit()
    return sid


def _seed_full_timeline(db: Database, sid: str) -> None:
    """Seed: 4 messages (user, assistant w/ 2 tool_calls, user, assistant)."""
    assert db.conn is not None
    rows: list[tuple[str, str, int, str, str]] = [
        ("m1", "user", 0, "2026-04-01T10:00:00Z", "what does ls do?"),
        ("m2", "assistant", 1, "2026-04-01T10:00:30Z", "let me check"),
        ("m3", "user", 2, "2026-04-01T10:01:00Z", "thanks"),
        ("m4", "assistant", 3, "2026-04-01T10:01:30Z", "you're welcome"),
    ]
    for mid, role, seq, ts, content in rows:
        db.conn.execute(
            "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, '{}')",
            (mid, sid, role, seq, ts, content),
        )
    # Two tool calls under m2.
    db.conn.execute(
        "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, started_at) "
        "VALUES (?, ?, ?, 0, ?, ?, ?)",
        ("tc1", "m2", sid, "Bash", '{"command": "ls /tmp"}', "2026-04-01T10:00:31Z"),
    )
    db.conn.execute(
        "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, started_at) "
        "VALUES (?, ?, ?, 1, ?, ?, ?)",
        ("tc2", "m2", sid, "Read", '{"path": "/tmp/foo.txt"}', "2026-04-01T10:00:32Z"),
    )
    db.conn.commit()


def test_inspect_session_returns_full_view(db: Database) -> None:
    sid = _seed_session(db)
    _seed_full_timeline(db, sid)

    view = inspect_session(db, sid)

    assert isinstance(view, SessionView)
    assert view.id == sid
    assert view.started_at == "2026-04-01T10:00:00Z"
    assert view.ended_at == "2026-04-01T11:30:00Z"
    assert view.project_path == "/work/foo"
    assert view.model == "claude-opus-4-7"
    assert view.git_branch == "main"
    assert len(view.messages) == 4

    # Order: by seq.
    assert [m.id for m in view.messages] == ["m1", "m2", "m3", "m4"]
    assert [m.role for m in view.messages] == ["user", "assistant", "user", "assistant"]

    # Tool calls only attached to m2.
    assert view.messages[0].tool_calls == ()
    assert view.messages[2].tool_calls == ()
    assert view.messages[3].tool_calls == ()

    m2 = view.messages[1]
    assert isinstance(m2, MessageView)
    assert len(m2.tool_calls) == 2
    assert isinstance(m2.tool_calls[0], ToolCallView)
    assert m2.tool_calls[0].name == "Bash"
    assert m2.tool_calls[0].id == "tc1"
    assert m2.tool_calls[0].input_json == '{"command": "ls /tmp"}'
    assert m2.tool_calls[1].name == "Read"
    assert m2.tool_calls[1].id == "tc2"


def test_inspect_session_empty_session(db: Database) -> None:
    sid = _seed_session(db, sid="empty-session-id")
    view = inspect_session(db, sid)
    assert view.messages == ()
    assert view.id == sid


def test_inspect_session_missing_id_raises(db: Database) -> None:
    _seed_session(db)
    with pytest.raises(RuntimeError, match="no session matches"):
        inspect_session(db, "no-such-id")


def test_resolve_session_id_exact(db: Database) -> None:
    sid = _seed_session(db)
    assert resolve_session_id(db, sid) == sid


def test_resolve_session_id_unique_prefix(db: Database) -> None:
    sid = _seed_session(db, sid="abcd1234-aaaa-bbbb-cccc-111122223333")
    # 8-char UUID prefix.
    assert resolve_session_id(db, "abcd1234") == sid
    # Even a 4-char prefix works if unique.
    assert resolve_session_id(db, "abcd") == sid


def test_resolve_session_id_ambiguous(db: Database) -> None:
    _seed_session(db, sid="abcd1111-aaaa-bbbb-cccc-111111111111", project="/p1")
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (2, '/data/bar.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
    )
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path) VALUES (?, 2, ?)",
        ("abcd2222-eeee-ffff-0000-222222222222", "/p2"),
    )
    db.conn.commit()

    with pytest.raises(RuntimeError, match="ambiguous") as excinfo:
        resolve_session_id(db, "abcd")
    msg = str(excinfo.value)
    assert "abcd1111" in msg
    assert "abcd2222" in msg


def test_resolve_session_id_ambiguous_truncates_with_marker(db: Database) -> None:
    """6+ matches share a prefix → message shows first 5 then `... (and more)`."""
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (1, '/data/foo.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
    )
    # Six sessions sharing the 2-char prefix "ab".
    sids = [
        "ab000001-aaaa-bbbb-cccc-000000000001",
        "ab000002-aaaa-bbbb-cccc-000000000002",
        "ab000003-aaaa-bbbb-cccc-000000000003",
        "ab000004-aaaa-bbbb-cccc-000000000004",
        "ab000005-aaaa-bbbb-cccc-000000000005",
        "ab000006-aaaa-bbbb-cccc-000000000006",
    ]
    for sid in sids:
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id) VALUES (?, 1)",
            (sid,),
        )
    db.conn.commit()

    with pytest.raises(RuntimeError, match="ambiguous") as excinfo:
        resolve_session_id(db, "ab")
    msg = str(excinfo.value)
    assert "(and more)" in msg
    # First five candidates should be present; the sixth must be hidden behind the marker.
    assert msg.count("ab00000") == 5


def test_resolve_session_id_underscore_prefix_does_not_wildcard(db: Database) -> None:
    """A prefix containing `_` must be treated literally, not as LIKE single-char wildcard."""
    _seed_session(db, sid="abcd1234-aaaa-bbbb-cccc-111122223333")
    with pytest.raises(RuntimeError, match="no session matches"):
        resolve_session_id(db, "_bcd1234")


def test_resolve_session_id_no_match(db: Database) -> None:
    _seed_session(db)
    with pytest.raises(RuntimeError, match="no session matches zzzz"):
        resolve_session_id(db, "zzzz")


def test_resolve_session_id_empty_db(db: Database) -> None:
    with pytest.raises(RuntimeError, match="no session matches anything"):
        resolve_session_id(db, "anything")


def test_inspect_session_works_with_null_metadata(db: Database) -> None:
    """Sessions with NULL header fields render with `None` placeholders."""
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (1, '/data/x.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
    )
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id) VALUES ('bare-session', 1)",
    )
    db.conn.commit()

    view = inspect_session(db, "bare-session")
    assert view.id == "bare-session"
    assert view.started_at is None
    assert view.ended_at is None
    assert view.project_path is None
    assert view.model is None
    assert view.git_branch is None
    assert view.messages == ()
