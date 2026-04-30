"""Tests for `convo.intake.orchestrator.index_file`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from convo.db import Database
from convo.intake.orchestrator import IndexResult, index_file

if TYPE_CHECKING:
    from pathlib import Path


_SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _user_text_record(uuid: str, text: str, *, parent: str | None = None) -> dict[str, object]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": _SESSION_ID,
        "timestamp": "2026-04-29T00:00:00Z",
        "cwd": "/Users/dev/develop/convo",
        "gitBranch": "main",
        "message": {"content": text},
    }


def _assistant_with_tool_use(uuid: str, *, parent: str | None = None) -> dict[str, object]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": _SESSION_ID,
        "timestamp": "2026-04-29T00:01:00Z",
        "cwd": "/Users/dev/develop/convo",
        "gitBranch": "main",
        "requestId": "req_1",
        "message": {
            "id": "msg_a1",
            "model": "claude-haiku-4-5",
            "content": [
                {"type": "text", "text": "running it"},
                {"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {"command": "ls"}},
            ],
        },
    }


def _user_with_tool_result(uuid: str, *, parent: str) -> dict[str, object]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": _SESSION_ID,
        "timestamp": "2026-04-29T00:02:00Z",
        "cwd": "/Users/dev/develop/convo",
        "gitBranch": "main",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_x",
                    "content": "exit 0",
                    "is_error": False,
                },
            ],
        },
    }


def _write_session(path: Path, records: list[dict[str, object]]) -> None:
    lines = [json.dumps(r, sort_keys=True) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def session_path(tmp_path: Path) -> Path:
    path = tmp_path / f"{_SESSION_ID}.jsonl"
    _write_session(
        path,
        [
            _user_text_record("u-1", "hello"),
            _assistant_with_tool_use("a-1", parent="u-1"),
            _user_with_tool_result("u-2", parent="a-1"),
        ],
    )
    return path


def _row_counts(db: Database) -> dict[str, int]:
    assert db.conn is not None
    return {
        "source_files": db.conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
        "sessions": db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "messages": db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "tool_calls": db.conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0],
        "tool_results": db.conn.execute("SELECT COUNT(*) FROM tool_results").fetchone()[0],
    }


def test_index_file_inserts_all_rows(db: Database, session_path: Path) -> None:
    result = index_file(db, session_path)
    assert isinstance(result, IndexResult)
    assert result.error is None
    assert result.skipped_reason is None
    assert result.source_file_id is not None
    assert result.inserted_rows == {"messages": 3, "tool_calls": 1, "tool_results": 1}

    counts = _row_counts(db)
    assert counts == {
        "source_files": 1,
        "sessions": 1,
        "messages": 3,
        "tool_calls": 1,
        "tool_results": 1,
    }

    assert db.conn is not None
    sess = db.conn.execute(
        "SELECT id, project_path, started_at, ended_at, model, git_branch FROM sessions",
    ).fetchone()
    assert sess["id"] == _SESSION_ID
    assert sess["project_path"] == "/Users/dev/develop/convo"
    assert sess["started_at"] == "2026-04-29T00:00:00Z"
    assert sess["ended_at"] == "2026-04-29T00:02:00Z"
    assert sess["model"] == "claude-haiku-4-5"
    assert sess["git_branch"] == "main"


def test_index_file_idempotent_on_repeat(db: Database, session_path: Path) -> None:
    first = index_file(db, session_path)
    assert first.skipped_reason is None
    before = _row_counts(db)

    second = index_file(db, session_path)
    assert second.skipped_reason == "unchanged"
    assert second.inserted_rows == {}
    assert second.source_file_id == first.source_file_id
    assert _row_counts(db) == before


def test_force_reindex_replaces_rows_cleanly(db: Database, session_path: Path) -> None:
    first = index_file(db, session_path)
    assert db.conn is not None
    indexed_at_1 = db.conn.execute(
        "SELECT last_indexed_at FROM source_files WHERE id = ?",
        (first.source_file_id,),
    ).fetchone()[0]

    second = index_file(db, session_path, force=True)
    assert second.error is None
    assert second.skipped_reason is None
    assert second.inserted_rows == {"messages": 3, "tool_calls": 1, "tool_results": 1}

    counts = _row_counts(db)
    assert counts == {
        "source_files": 1,
        "sessions": 1,
        "messages": 3,
        "tool_calls": 1,
        "tool_results": 1,
    }

    indexed_at_2 = db.conn.execute(
        "SELECT last_indexed_at FROM source_files WHERE id = ?",
        (second.source_file_id,),
    ).fetchone()[0]
    assert indexed_at_2 >= indexed_at_1

    orphans = db.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id NOT IN (SELECT id FROM sessions)",
    ).fetchone()[0]
    assert orphans == 0


def test_corrupted_jsonl_leaves_db_unchanged(db: Database, tmp_path: Path) -> None:
    path = tmp_path / f"{_SESSION_ID}.jsonl"
    valid_user = json.dumps(_user_text_record("u-1", "hi"), sort_keys=True)
    valid_assistant = json.dumps(_assistant_with_tool_use("a-1", parent="u-1"), sort_keys=True)
    garbage = '{"type":'
    path.write_text(f"{valid_user}\n{valid_assistant}\n{garbage}\n", encoding="utf-8")

    before = _row_counts(db)
    result = index_file(db, path)

    assert result.error is not None
    assert result.error_at_line == 3
    assert result.inserted_rows == {}
    assert result.source_file_id is None
    assert result.skipped_reason is None
    assert _row_counts(db) == before


def test_empty_file_returns_empty_skip(db: Database, tmp_path: Path) -> None:
    path = tmp_path / f"{_SESSION_ID}.jsonl"
    path.write_bytes(b"")

    result = index_file(db, path)
    assert result.skipped_reason == "empty"
    assert result.source_file_id is None
    assert result.inserted_rows == {}
    assert _row_counts(db)["source_files"] == 0


def test_blank_only_file_returns_empty_skip(db: Database, tmp_path: Path) -> None:
    path = tmp_path / f"{_SESSION_ID}.jsonl"
    path.write_text("\n  \n\n", encoding="utf-8")

    result = index_file(db, path)
    assert result.skipped_reason == "empty"
    assert _row_counts(db)["source_files"] == 0


def test_modified_file_reindexes_without_force(db: Database, tmp_path: Path) -> None:
    path = tmp_path / f"{_SESSION_ID}.jsonl"
    _write_session(path, [_user_text_record("u-1", "first")])
    first = index_file(db, path)
    assert first.inserted_rows["messages"] == 1

    _write_session(
        path,
        [_user_text_record("u-1", "first"), _user_text_record("u-2", "second")],
    )
    second = index_file(db, path)
    assert second.skipped_reason is None
    assert second.inserted_rows["messages"] == 2
    assert _row_counts(db)["source_files"] == 1
    assert _row_counts(db)["messages"] == 2


def test_index_file_raises_when_db_not_open(tmp_path: Path) -> None:
    path = tmp_path / f"{_SESSION_ID}.jsonl"
    _write_session(path, [_user_text_record("u-1", "hi")])
    closed_db = Database(tmp_path / "x.db")
    with pytest.raises(RuntimeError, match="not open"):
        index_file(closed_db, path)


def test_index_file_drops_cross_file_parent_uuid_to_null(db: Database, tmp_path: Path) -> None:
    """Regression: parentUuid pointing outside this file becomes NULL, not an FK error.

    Real Claude Code sessions resume from prior session ids, so a record's
    `parentUuid` can name a uuid that isn't in the current file. The mapper
    must NULL those references rather than insert an FK-violating row.
    """
    path = tmp_path / f"{_SESSION_ID}.jsonl"
    _write_session(
        path,
        [
            # Record 1 — root, no parent.
            _user_text_record("u-1", "hello"),
            # Record 2 — parent points at record 1 (in-file). Preserved.
            _user_text_record("u-2", "world", parent="u-1"),
            # Record 3 — parent points at a uuid not in this file. Drops to NULL.
            _user_text_record("u-3", "orphan", parent="u-from-prior-session"),
        ],
    )
    result = index_file(db, path)
    assert result.error is None
    assert result.inserted_rows["messages"] == 3

    assert db.conn is not None
    rows = db.conn.execute(
        "SELECT id, parent_id FROM messages ORDER BY seq",
    ).fetchall()
    assert rows[0]["id"] == "u-1"
    assert rows[0]["parent_id"] is None
    assert rows[1]["id"] == "u-2"
    assert rows[1]["parent_id"] == "u-1"
    assert rows[2]["id"] == "u-3"
    assert rows[2]["parent_id"] is None


def test_index_file_drops_cross_file_tool_result(db: Database, tmp_path: Path) -> None:
    """Regression: a tool_result whose tool_use_id isn't in this file is dropped.

    `tool_results.tool_call_id` is an FK to `tool_calls.id`. When Claude Code
    resumes from a prior session the user record may carry a tool_result for
    a tool_use that lives in the previous file. Such tool_results must be
    dropped silently rather than crash the whole file.
    """
    path = tmp_path / f"{_SESSION_ID}.jsonl"
    orphan: dict[str, object] = {
        "type": "user",
        "uuid": "u-orphan",
        "parentUuid": None,
        "sessionId": _SESSION_ID,
        "timestamp": "2026-04-29T00:00:00Z",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_from_prior_session",
                    "content": "stale output",
                    "is_error": False,
                },
            ],
        },
    }
    _write_session(path, [orphan])
    result = index_file(db, path)
    assert result.error is None
    assert result.inserted_rows["messages"] == 1
    assert result.inserted_rows["tool_results"] == 0


def test_index_file_handles_parent_appearing_after_child(db: Database, tmp_path: Path) -> None:
    """Regression: a child whose parent appears later in the same file inserts cleanly.

    Real Claude Code transcripts are not always topologically sorted by
    parentUuid — the parent record can come AFTER its child. The mapper's
    prescan knows the parent will exist, but per-row FK enforcement would
    reject the child before its parent has been inserted. The orchestrator
    sets `PRAGMA defer_foreign_keys = 1` so checks fire only at COMMIT.
    """
    path = tmp_path / f"{_SESSION_ID}.jsonl"
    _write_session(
        path,
        [
            # Child first — parent uuid points at u-2 which appears below.
            _user_text_record("u-1", "child", parent="u-2"),
            # Parent appears later in source order.
            _user_text_record("u-2", "parent"),
        ],
    )
    result = index_file(db, path)
    assert result.error is None
    assert result.inserted_rows["messages"] == 2

    assert db.conn is not None
    rows = db.conn.execute("SELECT id, parent_id FROM messages ORDER BY seq").fetchall()
    assert rows[0]["id"] == "u-1"
    assert rows[0]["parent_id"] == "u-2"
    assert rows[1]["id"] == "u-2"
    assert rows[1]["parent_id"] is None


def test_index_file_contains_db_error_in_result(db: Database, tmp_path: Path) -> None:
    """Containment: a sqlite3.IntegrityError must not escape `index_file`.

    We monkey-patch `INSERT_SQL["messages"]` to drop the `OR IGNORE` clause,
    then index two files that share a uuid. The second insert raises
    `sqlite3.IntegrityError`; the orchestrator must catch it, ROLLBACK, and
    return an `IndexResult` with `error` populated rather than propagating.
    """
    import convo.intake.mapper as mapper_mod  # noqa: PLC0415

    # File 1: a normal record.
    path1 = tmp_path / f"{_SESSION_ID}.jsonl"
    _write_session(path1, [_user_text_record("u-shared", "first")])
    first = index_file(db, path1)
    assert first.error is None

    # File 2: same uuid appears, in a different session (different stem).
    path2 = tmp_path / "22222222-3333-4444-5555-666666666666.jsonl"
    _write_session(path2, [_user_text_record("u-shared", "second")])

    # Strip OR IGNORE so the duplicate PK becomes a hard failure.
    original = mapper_mod.INSERT_SQL["messages"]
    try:
        mapper_mod.INSERT_SQL["messages"] = original.replace("INSERT OR IGNORE", "INSERT")
        result = index_file(db, path2)
    finally:
        mapper_mod.INSERT_SQL["messages"] = original

    # Containment: error is captured on the result, no exception escaped.
    assert result.error is not None
    assert "UNIQUE" in result.error or "PRIMARY KEY" in result.error.upper()
    assert result.source_file_id is None
    assert result.inserted_rows == {}

    # Transaction rolled back cleanly: file 2 produced no source_files row.
    assert db.conn is not None
    sf_count = db.conn.execute(
        "SELECT COUNT(*) FROM source_files WHERE path = ?", (str(path2),)
    ).fetchone()[0]
    assert sf_count == 0


def test_or_ignore_dedupes_cross_file_pk_collision(db: Database, tmp_path: Path) -> None:
    """Real-world case: the same Claude-Code uuid appears in two resumed sessions.

    With `INSERT OR IGNORE`, the second file's duplicate PK is silently
    skipped. The first row stays; the second is dropped. Either is
    acceptable — the run must NOT fail.
    """
    path1 = tmp_path / f"{_SESSION_ID}.jsonl"
    _write_session(path1, [_user_text_record("u-shared", "from session 1")])
    first = index_file(db, path1)
    assert first.error is None

    path2 = tmp_path / "33333333-4444-5555-6666-777777777777.jsonl"
    _write_session(path2, [_user_text_record("u-shared", "from session 2")])
    second = index_file(db, path2)
    assert second.error is None
    # The duplicate row was IGNOREd, but the file is still indexed and
    # `source_files` records its presence.
    assert second.source_file_id is not None

    assert db.conn is not None
    msg_count = db.conn.execute("SELECT COUNT(*) FROM messages WHERE id = 'u-shared'").fetchone()[0]
    assert msg_count == 1


def test_index_file_contains_unicode_decode_error(db: Database, tmp_path: Path) -> None:
    """Containment: a malformed UTF-8 byte sequence in a JSONL must not escape.

    `parse_file` decodes UTF-8 strictly; a bad byte sequence raises
    `UnicodeDecodeError`. The orchestrator must catch it, ROLLBACK, and
    return an `IndexResult` with `error` populated rather than propagating
    the exception (which would abort the entire tree run).
    """
    path = tmp_path / f"{_SESSION_ID}.jsonl"
    path.write_bytes(b"\xff\xfe\x00invalid\n")

    before = _row_counts(db)
    result = index_file(db, path)

    assert result.error is not None
    assert result.error_at_line is None
    assert result.source_file_id is None
    assert result.inserted_rows == {}
    assert _row_counts(db) == before


def test_index_file_no_orphan_foreign_keys(db: Database, session_path: Path) -> None:
    """Smoke: every persisted FK-bearing column either is NULL or resolves in-DB."""
    index_file(db, session_path)
    assert db.conn is not None

    orphan_parents = db.conn.execute(
        "SELECT COUNT(*) FROM messages "
        "WHERE parent_id IS NOT NULL AND parent_id NOT IN (SELECT id FROM messages)",
    ).fetchone()[0]
    assert orphan_parents == 0

    orphan_tc_messages = db.conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE message_id NOT IN (SELECT id FROM messages)",
    ).fetchone()[0]
    assert orphan_tc_messages == 0

    orphan_tr_calls = db.conn.execute(
        "SELECT COUNT(*) FROM tool_results WHERE tool_call_id NOT IN (SELECT id FROM tool_calls)",
    ).fetchone()[0]
    assert orphan_tr_calls == 0

    orphan_tr_messages = db.conn.execute(
        "SELECT COUNT(*) FROM tool_results "
        "WHERE message_id IS NOT NULL AND message_id NOT IN (SELECT id FROM messages)",
    ).fetchone()[0]
    assert orphan_tr_messages == 0
