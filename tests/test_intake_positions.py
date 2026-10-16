"""Stored message positions: the intake maintainer and its index_file wiring."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from convo.db import Database
from convo.intake.orchestrator import index_file
from convo.intake.positions import assign_positions
from convo.read.inspect import MESSAGE_ORDER_BY

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

_SID = "90517100-0000-4000-8000-000000000001"
_EXPECTED = (
    "SELECT id, ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY "  # noqa: S608
    + MESSAGE_ORDER_BY
    + ") FROM messages ORDER BY id"
)
_STORED = "SELECT message_id, position FROM message_positions ORDER BY message_id"


def _expected(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [(r[0], r[1]) for r in conn.execute(_EXPECTED)]


def _stored(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [(r[0], r[1]) for r in conn.execute(_STORED)]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with Database(tmp_path / "convo.db") as db:
        assert db.conn is not None
        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (1, '/synthetic/a.jsonl', 0, 0, '2026-10-01T00:00:00Z')"
        )
        db.conn.execute("INSERT INTO sessions(id, source_file_id) VALUES (?, 1)", (_SID,))
        yield db.conn


def _insert(conn: sqlite3.Connection, rows: list[tuple[str, int, str | None]]) -> None:
    conn.executemany(
        "INSERT INTO messages(id, session_id, role, seq, timestamp, raw_json) "
        "VALUES (?, ?, 'user', ?, ?, '{}')",
        [(mid, _SID, seq, ts) for mid, seq, ts in rows],
    )


def _assign(conn: sqlite3.Connection, min_new_seq: int, source_count: int = 1) -> str:
    """Run the maintainer; return which path it took."""
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        assign_positions(conn, {_SID: (min_new_seq, source_count)})
    finally:
        conn.set_trace_callback(None)
    writes = [s for s in statements if s.startswith("INSERT INTO message_positions")]
    assert len(writes) == 1, statements
    return "recompute" if "ON CONFLICT" in writes[0] else "append"


_FIRST = [("m-0", 0, "2026-10-01T10:00:00Z"), ("m-1", 1, None), ("m-2", 2, "2026-10-01T10:00:02Z")]


def test_append_numbers_only_the_new_rows(conn: sqlite3.Connection) -> None:
    _insert(conn, _FIRST)
    assert _assign(conn, 0) == "append"
    # Two new rows tie on seq and timestamp; the id breaks the tie.
    _insert(conn, [("m-4b", 3, "2026-10-01T10:00:03Z"), ("m-4a", 3, "2026-10-01T10:00:03Z")])
    assert _assign(conn, 3) == "append"
    assert _stored(conn) == _expected(conn)
    assert dict(_stored(conn))["m-4a"] == 4


def test_rows_landing_among_existing_ones_renumber(conn: sqlite3.Connection) -> None:
    _insert(conn, _FIRST)
    _assign(conn, 0)
    # Same seq as m-2 with an earlier timestamp: sorts before it and shifts it.
    _insert(conn, [("m-x", 2, "2026-10-01T10:00:01Z")])
    assert _assign(conn, 2) == "recompute"
    assert _stored(conn) == _expected(conn)
    assert dict(_stored(conn))["m-2"] == 4


def test_multi_source_session_renumbers(conn: sqlite3.Connection) -> None:
    _insert(conn, _FIRST)
    _assign(conn, 0)
    _insert(conn, [("m-3", 3, None)])
    assert _assign(conn, 3, source_count=2) == "recompute"
    assert _stored(conn) == _expected(conn)


def test_unnumbered_rows_before_the_batch_renumber(conn: sqlite3.Connection) -> None:
    _insert(conn, _FIRST)  # written without the maintainer
    _insert(conn, [("m-3", 3, None)])
    assert _assign(conn, 3) == "recompute"
    assert _stored(conn) == _expected(conn)


def _record(uuid: str, sid: str, ts: str, text: str) -> dict[str, object]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": sid,
        "timestamp": ts,
        "cwd": "/work/positions",
        "message": {"content": text},
    }


def _write(path: Path, records: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _session_file(path: Path, prefix: str, minutes: list[int]) -> Path:
    return _write(
        path,
        [
            _record(f"{prefix}-{i}", _SID, f"2026-10-01T10:{m:02d}:00Z", f"{prefix} {i}")
            for i, m in enumerate(minutes)
        ],
    )


def test_index_file_stores_inspect_positions(tmp_path: Path) -> None:
    jsonl = _session_file(tmp_path / "proj" / f"{_SID}.jsonl", "a", [0, 1, 2, 3])
    with Database(tmp_path / "convo.db") as db:
        assert db.conn is not None
        assert index_file(db, jsonl).error is None
        assert len(_stored(db.conn)) == 4
        assert _stored(db.conn) == _expected(db.conn)


def test_second_source_file_renumbers_the_session(tmp_path: Path) -> None:
    """Two files for one session id: seq restarts at 0, so rows interleave by timestamp."""
    first = _session_file(tmp_path / "p1" / f"{_SID}.jsonl", "a", [0, 2, 4])
    second = _session_file(tmp_path / "p2" / f"{_SID}.jsonl", "b", [1, 3])
    with Database(tmp_path / "convo.db") as db:
        assert db.conn is not None
        assert index_file(db, first).error is None
        assert index_file(db, second).error is None
        assert len(_stored(db.conn)) == 5
        assert _stored(db.conn) == _expected(db.conn)


def test_full_reindex_and_delete_keep_positions_in_step(tmp_path: Path) -> None:
    jsonl = _session_file(tmp_path / "proj" / f"{_SID}.jsonl", "a", [0, 1, 2])
    with Database(tmp_path / "convo.db") as db:
        assert db.conn is not None
        index_file(db, jsonl)
        _session_file(jsonl, "a", [0, 1, 2, 3, 4])
        assert index_file(db, jsonl, force=True).error is None
        assert len(_stored(db.conn)) == 5
        assert _stored(db.conn) == _expected(db.conn)
        db.conn.execute("DELETE FROM source_files")
        db.conn.commit()
        assert _stored(db.conn) == []
