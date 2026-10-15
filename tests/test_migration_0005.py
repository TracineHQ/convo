"""Migration 0005: FK child-key indexes and FTS update triggers narrowed to text columns."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from convo.db import Database
from tests._seed import (
    build_db_at_version,
    seed_full_chain,
    seed_guard_decision,
    seed_source_file,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _index_columns(conn: sqlite3.Connection, table: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in conn.execute(f"PRAGMA index_list('{table}')").fetchall():
        name = row[1]
        out[name] = [r[2] for r in conn.execute(f"PRAGMA index_info('{name}')").fetchall()]
    return out


def _fts_state(conn: sqlite3.Connection, fts: str) -> tuple[str | None, str | None]:
    """Snapshot an FTS5 index's shadow tables; any FTS write changes them."""
    data = conn.execute(
        f"SELECT group_concat(id || ':' || hex(block), ',') FROM {fts}_data",  # noqa: S608
    ).fetchone()[0]
    docsize = conn.execute(
        f"SELECT group_concat(id || ':' || hex(sz), ',') FROM {fts}_docsize",  # noqa: S608
    ).fetchone()[0]
    return data, docsize


def test_fresh_db_indexes_fk_child_columns(db: Database) -> None:
    assert db.conn is not None
    assert _index_columns(db.conn, "messages")["idx_messages_parent"] == ["parent_id"]
    assert _index_columns(db.conn, "tool_results")["idx_tool_results_message"] == ["message_id"]


@pytest.mark.parametrize(
    ("sql", "index"),
    [
        ("SELECT 1 FROM messages WHERE parent_id = ?", "idx_messages_parent"),
        ("SELECT 1 FROM tool_results WHERE message_id = ?", "idx_tool_results_message"),
    ],
)
def test_child_key_lookups_use_index(db: Database, sql: str, index: str) -> None:
    assert db.conn is not None
    plan = " ".join(str(r[3]) for r in db.conn.execute(f"EXPLAIN QUERY PLAN {sql}", ("x",)))
    assert index in plan


@pytest.mark.parametrize(
    ("table", "fts", "update_sql"),
    [
        ("messages", "messages_fts", "UPDATE messages SET seq = seq + 1, parent_id = NULL"),
        ("tool_calls", "tool_calls_fts", "UPDATE tool_calls SET seq = 7, duration_ms = 42"),
        ("tool_results", "tool_results_fts", "UPDATE tool_results SET is_error = 1"),
        ("guard_decisions", "guard_decisions_fts", "UPDATE guard_decisions SET cwd = '/moved'"),
    ],
)
def test_non_fts_column_update_leaves_fts_untouched(
    db: Database,
    table: str,
    fts: str,
    update_sql: str,
) -> None:
    assert db.conn is not None
    seed_full_chain(db, message_content="kafka lag", tool_output_text="all partitions healthy")
    seed_guard_decision(db, seed_source_file(db, path="/data/guard.jsonl"), reason="blocked rm")
    before = _fts_state(db.conn, fts)
    changes_before = db.conn.total_changes

    db.conn.execute(update_sql)
    db.conn.commit()

    assert db.conn.total_changes - changes_before == 1, f"{table} update fired an FTS write"
    assert _fts_state(db.conn, fts) == before


@pytest.mark.parametrize(
    ("update_sql", "fts", "old", "new"),
    [
        ("UPDATE messages SET content = 'zebracorn'", "messages_fts", "kafka", "zebracorn"),
        (
            "UPDATE tool_calls SET input_json = 'zebracorn'",
            "tool_calls_fts",
            "command",
            "zebracorn",
        ),
        ("UPDATE tool_calls SET name = 'Zebracorn'", "tool_calls_fts", "Bash", "zebracorn"),
        (
            "UPDATE tool_results SET output_text = 'zebracorn'",
            "tool_results_fts",
            "healthy",
            "zebracorn",
        ),
        (
            "UPDATE guard_decisions SET reason = 'zebracorn'",
            "guard_decisions_fts",
            "blocked",
            "zebracorn",
        ),
        (
            "UPDATE guard_decisions SET command_excerpt = 'zebracorn'",
            "guard_decisions_fts",
            "rm -rf",
            "zebracorn",
        ),
    ],
)
def test_fts_column_update_still_reindexes(
    db: Database,
    update_sql: str,
    fts: str,
    old: str,
    new: str,
) -> None:
    assert db.conn is not None
    conn = db.conn
    seed_full_chain(
        db,
        message_content="kafka lag",
        tool_input_json='{"command": "ls"}',
        tool_output_text="all partitions healthy",
    )
    gid = seed_guard_decision(db, seed_source_file(db, path="/data/guard.jsonl"), reason="blocked")
    conn.execute("UPDATE guard_decisions SET command_excerpt = 'rm -rf /' WHERE id = ?", (gid,))
    conn.commit()

    def hits(term: str) -> int:
        row = conn.execute(
            f"SELECT count(*) FROM {fts} WHERE {fts} MATCH ?",  # noqa: S608
            (f'"{term}"',),
        ).fetchone()
        return int(row[0])

    assert hits(old) == 1
    conn.execute(update_sql)
    conn.commit()
    assert hits(new) == 1
    assert hits(old) == 0


def _seed_v4(raw: sqlite3.Connection, *, sessions: int = 3, per_session: int = 40) -> None:
    ts = "2026-09-01T00:00:00Z"
    for s in range(sessions):
        raw.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (?, ?, 1, 1, ?)",
            (s + 1, f"/synthetic/{s}.jsonl", ts),
        )
        sid = f"sess-{s}"
        raw.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at) "
            "VALUES (?, ?, ?, ?)",
            (sid, s + 1, "/synthetic", ts),
        )
        for n in range(per_session):
            mid = f"{sid}-m{n}"
            parent = f"{sid}-m{n - 1}" if n else None
            raw.execute(
                "INSERT INTO messages(id, session_id, parent_id, role, seq, timestamp, content, "
                "raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')",
                (mid, sid, parent, "user" if n % 2 else "assistant", n, ts, f"needle {sid} {n}"),
            )
            if n % 2 == 0:
                tcid = f"{sid}-tc{n}"
                raw.execute(
                    "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json) "
                    "VALUES (?, ?, ?, 0, 'Bash', ?)",
                    (tcid, mid, sid, f'{{"command": "echo {n}"}}'),
                )
                raw.execute(
                    "INSERT INTO tool_results(tool_call_id, message_id, output_text) "
                    "VALUES (?, ?, ?)",
                    (tcid, mid, f"output {sid} {n}"),
                )
    raw.execute(
        "INSERT INTO source_files(id, path, kind, size, mtime_ns, last_indexed_at) "
        "VALUES (99, '/synthetic/guard.jsonl', 'guard_decisions', 1, 1, ?)",
        (ts,),
    )
    raw.execute(
        "INSERT INTO guard_decisions(source_file_id, line_no, schema_version, mode, timestamp, "
        "hook_id, event, decision, reason, session_id, raw_json) "
        "VALUES (99, 1, 1, 'lockdown', ?, 'guard.bash', 'PreToolUse', 'deny', 'needle reason', "
        "'sess-0', '{}')",
        (ts,),
    )
    raw.commit()


_COUNTED = ("source_files", "sessions", "messages", "tool_calls", "tool_results", "guard_decisions")


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])  # noqa: S608
        for t in _COUNTED
    }


def test_0005_replays_on_populated_v4_db(db_path: Path) -> None:
    raw = build_db_at_version(db_path, 4)
    _seed_v4(raw)
    before = _counts(raw)
    raw.close()

    # Exactly the 2.1.x writer path: open() sets foreign_keys=ON, then migrate()
    # runs 0005 inside BEGIN EXCLUSIVE.
    with Database(db_path) as db:
        assert db.conn is not None
        conn = db.conn
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert _counts(conn) == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for fts in ("messages_fts", "tool_calls_fts", "tool_results_fts", "guard_decisions_fts"):
            conn.execute(f"INSERT INTO {fts}({fts}) VALUES ('integrity-check')")  # noqa: S608
        assert _index_columns(conn, "messages")["idx_messages_parent"] == ["parent_id"]
        assert _index_columns(conn, "tool_results")["idx_tool_results_message"] == ["message_id"]
        triggers = dict(
            conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall(),
        )
        assert "AFTER UPDATE OF content ON messages" in triggers["messages_au"]
        assert "AFTER UPDATE OF name, input_json ON tool_calls" in triggers["tool_calls_au"]
        assert "AFTER UPDATE OF output_text ON tool_results" in triggers["tool_results_au"]
        assert (
            "AFTER UPDATE OF reason, command_excerpt ON guard_decisions"
            in triggers["guard_decisions_au"]
        )
        hits = conn.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'needle'",
        ).fetchone()[0]
        assert hits == before["messages"]
        applied = conn.execute(
            "SELECT filename FROM schema_migrations WHERE version = 5",
        ).fetchone()[0]
        assert applied == "0005_fk_child_indexes.sql"

        # Deleting a file still cascades and nulls cross-file parents.
        conn.execute("DELETE FROM source_files WHERE id = 1")
        conn.commit()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('integrity-check')")
        left = conn.execute("SELECT count(*) FROM messages WHERE session_id = 'sess-0'").fetchone()
        assert left[0] == 0
