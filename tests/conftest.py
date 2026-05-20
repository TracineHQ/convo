"""Shared pytest fixtures for convo tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from convo.db import Database

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "convo.db"


@pytest.fixture
def db(db_path: Path) -> Iterator[Database]:
    with Database(db_path) as database:
        yield database


def _ts(offset: timedelta) -> str:
    return (datetime.now(UTC) - offset).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def seeded_db_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Return path to a SQLite DB seeded with a small realistic corpus.

    Contains at least 3 sessions across 4 projects, 5+ messages per session,
    tool calls (Bash/Read/Edit), and at least 1 tool_result.
    """
    path = tmp_path_factory.mktemp("seeded") / "convo.db"
    with Database(path) as db:
        assert db.conn is not None
        conn = db.conn

        ts_now = _ts(timedelta(seconds=0))
        ts_7d = _ts(timedelta(days=7))
        ts_30d = _ts(timedelta(days=30))

        projects = [
            "/Users/dev/develop/tracine-ops",
            "/Users/dev/develop/convo",
            "/Users/dev/develop/uu-rolecapacity-bff",
            "/Users/dev/develop/ai-toolkit",
        ]

        # source files
        for i, proj in enumerate(projects, start=1):
            conn.execute(
                "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
                "VALUES (?, ?, 0, 0, ?)",
                (i, f"{proj}/transcript.jsonl", ts_now),
            )

        # 3 sessions across 3 projects with varied timestamps
        sessions = [
            ("sess-1", 1, projects[0], ts_now),
            ("sess-2", 2, projects[1], ts_7d),
            ("sess-3", 3, projects[2], ts_30d),
        ]
        for sid, sfid, proj, started in sessions:
            conn.execute(
                "INSERT INTO sessions(id, source_file_id, project_path, started_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, sfid, proj, started),
            )

        # 5+ messages per session with varied roles and content
        messages: list[tuple[str, str, int, str, str, str]] = [
            # sess-1 (recent): kafka mentions + cross-examine for hyphen test
            ("m1-1", "sess-1", 0, "user", "kafka pipeline setup for tracine-ops project", ts_now),
            ("m1-2", "sess-1", 1, "assistant", "Sure, I'll cross-examine the kafka config", ts_now),
            ("m1-3", "sess-1", 2, "user", "check the kafka consumer group lag", ts_now),
            ("m1-4", "sess-1", 3, "assistant", "kafka lag is zero, all good", ts_now),
            ("m1-5", "sess-1", 4, "user", "run the integration tests now", ts_now),
            # sess-2 (7 days ago)
            ("m2-1", "sess-2", 0, "user", "kafka topic configuration review", ts_7d),
            ("m2-2", "sess-2", 1, "assistant", "reviewing kafka topic partitions", ts_7d),
            ("m2-3", "sess-2", 2, "user", "also check the database migrations", ts_7d),
            ("m2-4", "sess-2", 3, "assistant", "migrations look clean", ts_7d),
            ("m2-5", "sess-2", 4, "user", "deploy to staging when ready", ts_7d),
            # sess-3 (30 days ago)
            ("m3-1", "sess-3", 0, "user", "initial uu-rolecapacity setup", ts_30d),
            ("m3-2", "sess-3", 1, "assistant", "setting up the role capacity service", ts_30d),
            ("m3-3", "sess-3", 2, "user", "add kafka event bus support", ts_30d),
            ("m3-4", "sess-3", 3, "assistant", "kafka event bus integration complete", ts_30d),
            ("m3-5", "sess-3", 4, "user", "write the tests", ts_30d),
        ]
        for mid, sid, seq, role, content, ts in messages:
            conn.execute(
                "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, '{}')",
                (mid, sid, role, seq, ts, content),
            )

        # tool_calls across sessions with varied names
        conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, started_at) "
            "VALUES (?, ?, ?, 0, ?, ?, ?)",
            ("tc-1", "m1-5", "sess-1", "Bash", '{"command": "uv run pytest"}', ts_now),
        )
        conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, started_at) "
            "VALUES (?, ?, ?, 0, ?, ?, ?)",
            ("tc-2", "m2-3", "sess-2", "Read", '{"path": "migrations/0001_init.sql"}', ts_7d),
        )
        conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, started_at) "
            "VALUES (?, ?, ?, 0, ?, ?, ?)",
            ("tc-3", "m3-5", "sess-3", "Edit", '{"path": "tests/conftest.py"}', ts_30d),
        )

        # tool_result with kafka content
        conn.execute(
            "INSERT INTO tool_results(tool_call_id, message_id, output_text) VALUES (?, ?, ?)",
            ("tc-1", "m1-5", "kafka consumer group lag: 0 -- all partitions healthy"),
        )

        conn.commit()

    return str(path)


@pytest.fixture(scope="module")
def seeded_session_id(seeded_db_path: str) -> str:
    """Return the id of the first (lowest id) session in the seeded DB."""
    conn = sqlite3.connect(seeded_db_path)
    try:
        row = conn.execute("SELECT id FROM sessions ORDER BY id LIMIT 1").fetchone()
        assert row is not None
        return str(row[0])
    finally:
        conn.close()


@pytest.fixture(scope="module")
def seeded_long_session_id(seeded_db_path: str) -> str:
    """Append a session with 60+ messages to the seeded DB; return its id.

    Used to verify default cap-at-50 truncation in inspect tests.
    """
    sid = "sess-long"
    conn = sqlite3.connect(seeded_db_path)
    try:
        # source file for this session
        ts_now = _ts(timedelta(seconds=0))
        conn.execute(
            "INSERT OR IGNORE INTO source_files(path, size, mtime_ns, last_indexed_at) "
            "VALUES (?, 0, 0, ?)",
            ("/Users/dev/develop/ai-toolkit/long.jsonl", ts_now),
        )
        sfid = conn.execute(
            "SELECT id FROM source_files WHERE path = ?",
            ("/Users/dev/develop/ai-toolkit/long.jsonl",),
        ).fetchone()[0]

        conn.execute(
            "INSERT OR IGNORE INTO sessions(id, source_file_id, project_path, started_at) "
            "VALUES (?, ?, ?, ?)",
            (sid, sfid, "/Users/dev/develop/ai-toolkit", ts_now),
        )
        # check how many messages already exist for this session
        existing = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]

        roles = ["user", "assistant"]
        for i in range(existing, 62):
            conn.execute(
                "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, '{}')",
                (
                    f"m-long-{i}",
                    sid,
                    roles[i % 2],
                    i,
                    ts_now,
                    f"long session message {i}",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return sid
