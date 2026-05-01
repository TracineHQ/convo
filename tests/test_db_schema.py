"""Schema-level tests: CHECK constraints, FK enforcement, cascades."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from convo.db import Database


_NOW = "2026-04-29T00:00:00Z"


def _seed_source_file(db: Database, *, path: str = "/tmp/x.jsonl") -> int:
    assert db.conn is not None
    cur = db.conn.execute(
        "INSERT INTO source_files(path, size, mtime_ns, last_indexed_at) VALUES (?, 0, 0, ?)",
        (path, _NOW),
    )
    db.conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _seed_session(db: Database, source_file_id: int, *, sid: str = "s1") -> str:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id) VALUES (?, ?)",
        (sid, source_file_id),
    )
    db.conn.commit()
    return sid


def _seed_message(
    db: Database,
    session_id: str,
    *,
    mid: str = "m1",
    parent_id: str | None = None,
    content: str = "hi",
    seq: int = 0,
) -> str:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO messages(id, session_id, parent_id, role, seq, content, raw_json) "
        "VALUES (?, ?, ?, 'user', ?, ?, '{}')",
        (mid, session_id, parent_id, seq, content),
    )
    db.conn.commit()
    return mid


def _seed_tool_call(
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


def _seed_tool_result(
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


def test_source_files_kind_default_is_transcript(db: Database) -> None:
    sfid = _seed_source_file(db)
    assert db.conn is not None
    kind = db.conn.execute(
        "SELECT kind FROM source_files WHERE id = ?",
        (sfid,),
    ).fetchone()[0]
    assert kind == "transcript"


def test_source_files_kind_rejects_bogus(db: Database) -> None:
    assert db.conn is not None
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.conn.execute(
            "INSERT INTO source_files(path, kind, size, mtime_ns, last_indexed_at) "
            "VALUES ('/b.jsonl', 'bogus', 0, 0, ?)",
            (_NOW,),
        )


def test_full_insert_flow_counts(db: Database) -> None:
    sfid = _seed_source_file(db)
    sid = _seed_session(db, sfid)
    mid = _seed_message(db, sid)
    tcid = _seed_tool_call(db, mid, sid)
    _seed_tool_result(db, tcid)

    assert db.conn is not None
    counts = {
        t: db.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in ("source_files", "sessions", "messages", "tool_calls", "tool_results")
    }
    assert counts == {
        "source_files": 1,
        "sessions": 1,
        "messages": 1,
        "tool_calls": 1,
        "tool_results": 1,
    }


def test_session_with_unknown_source_file_id_raises_fk(db: Database) -> None:
    assert db.conn is not None
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id) VALUES ('orphan', 999)",
        )


def test_cascade_delete_through_chain(db: Database) -> None:
    sfid = _seed_source_file(db)
    sid = _seed_session(db, sfid)
    mid = _seed_message(db, sid)
    tcid = _seed_tool_call(db, mid, sid)
    _seed_tool_result(db, tcid)

    assert db.conn is not None
    db.conn.execute("DELETE FROM source_files WHERE id = ?", (sfid,))
    db.conn.commit()

    for table in ("sessions", "messages", "tool_calls", "tool_results"):
        count = db.conn.execute(
            f"SELECT count(*) FROM {table}",  # noqa: S608
        ).fetchone()[0]
        assert count == 0, f"{table} not cascaded"


def test_message_parent_set_null_on_delete(db: Database) -> None:
    sfid = _seed_source_file(db)
    sid = _seed_session(db, sfid)
    _seed_message(db, sid, mid="m1", seq=0)
    _seed_message(db, sid, mid="m2", parent_id="m1", seq=1)

    assert db.conn is not None
    db.conn.execute("DELETE FROM messages WHERE id = 'm1'")
    db.conn.commit()

    row = db.conn.execute(
        "SELECT parent_id FROM messages WHERE id = 'm2'",
    ).fetchone()
    assert row is not None
    assert row[0] is None
