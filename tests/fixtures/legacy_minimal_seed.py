"""Build a minimal legacy convo DB for transform/validation tests.

Schema lifted verbatim from the legacy TracineHQ/claude-skills convo
package (`convo/src/convo/schema.py`). Insert content covers every edge
case the new transform functions must handle:

- 3 conversations (one normal, one with `is_subagent=1`, one whose
  matching `indexed_files` row is intentionally missing)
- 5 indexed_files rows
- multiple messages (mix of `user`, `assistant`, `system`, plus one
  intentional bad role to exercise the drop branch)
- multiple tool_calls (one with NULL `input_json`, one whose timestamp
  matches two assistant messages — exercises the lowest-id tie-break,
  one whose timestamp matches no assistant message — exercises the
  drop branch)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

_LEGACY_SCHEMA = """
CREATE TABLE conversations (
    id              TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    path            TEXT NOT NULL UNIQUE,
    is_subagent     BOOLEAN DEFAULT 0,
    parent_id       TEXT,
    started_at      TEXT,
    ended_at        TEXT,
    model           TEXT,
    message_count   INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    cwd             TEXT,
    git_branch      TEXT
);

CREATE TABLE tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    timestamp       TEXT,
    tool_name       TEXT NOT NULL,
    input_json      TEXT,
    command         TEXT,
    file_path       TEXT,
    pattern         TEXT,
    description     TEXT,
    has_newlines    BOOLEAN DEFAULT 0,
    caller_type     TEXT DEFAULT 'main',
    sequence_num    INTEGER
);

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    timestamp       TEXT,
    role            TEXT NOT NULL,
    content_length  INTEGER,
    tool_use_count  INTEGER DEFAULT 0,
    sequence_num    INTEGER
);

CREATE TABLE indexed_files (
    path        TEXT PRIMARY KEY,
    mtime_ns    INTEGER NOT NULL,
    size_bytes  INTEGER NOT NULL,
    indexed_at  TEXT NOT NULL
);
"""


def seed_legacy(conn: sqlite3.Connection) -> None:
    """Populate `conn` with a minimal legacy DB exercising edge cases."""
    conn.executescript(_LEGACY_SCHEMA)

    # 5 indexed_files; conv-C's path intentionally absent so its session drops.
    conn.executemany(
        "INSERT INTO indexed_files(path, mtime_ns, size_bytes, indexed_at) VALUES (?, ?, ?, ?)",
        [
            ("/p1/a.jsonl", 1700000000_000000000, 1024, "2026-04-01T00:00:00Z"),
            ("/p1/b.jsonl", 1700000001_000000000, 2048, "2026-04-01T00:01:00Z"),
            ("/p2/c.jsonl", 1700000002_000000000, 4096, "2026-04-01T00:02:00Z"),
            ("/p2/d.jsonl", 1700000003_000000000, 8192, "2026-04-01T00:03:00Z"),
            ("/p3/e.jsonl", 1700000004_000000000, 16384, "2026-04-01T00:04:00Z"),
        ],
    )

    # 3 conversations.
    conn.executemany(
        "INSERT INTO conversations("
        "id, project, path, is_subagent, parent_id, started_at, ended_at, "
        "model, message_count, tool_call_count, cwd, git_branch) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "conv-A",
                "p1",
                "/p1/a.jsonl",
                0,
                None,
                "2026-04-01T00:10:00Z",
                "2026-04-01T00:20:00Z",
                "claude-opus-4-7",
                4,
                3,
                "/work/p1",
                "main",
            ),
            (
                "conv-B",
                "p1",
                "/p1/b.jsonl",
                1,
                "conv-A",
                "2026-04-01T00:11:00Z",
                None,
                "claude-haiku-4-5",
                1,
                0,
                "/work/p1",
                "feat/x",
            ),
            (
                "conv-C",
                "p3",
                "/orphan/missing.jsonl",
                0,
                None,
                "2026-04-01T00:12:00Z",
                None,
                "claude-sonnet-4-6",
                1,
                0,
                None,
                None,
            ),
        ],
    )

    # Messages: mix of roles, plus one bad role.
    conn.executemany(
        "INSERT INTO messages("
        "id, conversation_id, timestamp, role, content_length, "
        "tool_use_count, sequence_num) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            # conv-A: user, assistant, assistant (same ts), system
            (1, "conv-A", "2026-04-01T00:10:01Z", "user", 42, 0, 0),
            (2, "conv-A", "2026-04-01T00:10:02Z", "assistant", 100, 1, 1),
            (3, "conv-A", "2026-04-01T00:10:02Z", "assistant", 80, 1, 2),
            (4, "conv-A", "2026-04-01T00:10:03Z", "system", 10, 0, 3),
            # conv-B: subagent assistant
            (5, "conv-B", "2026-04-01T00:11:01Z", "assistant", 50, 0, 0),
            # conv-C: bad role (drop)
            (6, "conv-C", "2026-04-01T00:12:01Z", "weird", 5, 0, 0),
        ],
    )

    # Tool calls:
    # tc1: matches msg 2/3 (same ts) — tie-broken to msg 2 (lowest id)
    # tc2: NULL input_json
    # tc3: timestamp doesn't match any assistant message (drops)
    conn.executemany(
        "INSERT INTO tool_calls("
        "id, conversation_id, timestamp, tool_name, input_json, command, "
        "file_path, pattern, description, has_newlines, caller_type, "
        "sequence_num) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                1,
                "conv-A",
                "2026-04-01T00:10:02Z",
                "Bash",
                '{"command":"echo hi"}',
                "echo hi",
                None,
                None,
                None,
                0,
                "main",
                10,
            ),
            (
                2,
                "conv-A",
                "2026-04-01T00:10:02Z",
                "Read",
                None,
                None,
                "/p1/a.jsonl",
                None,
                None,
                1,
                "main",
                11,
            ),
            (
                3,
                "conv-A",
                "9999-01-01T00:00:00Z",  # no matching assistant message
                "Grep",
                '{"pattern":"foo"}',
                None,
                None,
                "foo",
                None,
                0,
                "main",
                12,
            ),
        ],
    )
    conn.commit()
