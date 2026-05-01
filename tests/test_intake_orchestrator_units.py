"""Service-isolated unit tests for `convo.intake.orchestrator`.

These tests autospec `sqlite3.Connection`/`Cursor` and patch the intake
helpers (`parse_file`, `compute_file_signature`, `_is_empty_file`) so the
orchestrator state machine can be exercised without touching a real SQLite
database. They assert exact SQL call sequences and transaction semantics.

Integration coverage lives in `tests/test_intake_orchestrator.py`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from convo.intake import orchestrator as orch
from convo.intake.orchestrator import IndexResult, index_file
from convo.intake.parser import IntakeParseError
from convo.intake.records import UserMessage

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


_PATH = Path("/fake/projects/slug/abcdef.jsonl")
_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SIZE = 1234
_MTIME_NS = 1_700_000_000_000_000_000


def _make_conn(mocker: MockerFixture) -> Any:
    """Autospec'd `sqlite3.Connection` instance suitable for orchestrator use."""
    return mocker.create_autospec(sqlite3.Connection, spec_set=True, instance=True)


def _make_cursor(
    mocker: MockerFixture,
    *,
    fetchone: Any = None,
    lastrowid: int | None = None,
) -> Any:
    cur = mocker.create_autospec(sqlite3.Cursor, spec_set=True, instance=True)
    cur.fetchone.return_value = fetchone
    # `lastrowid` is a property on the real type; the autospec exposes it as a
    # Mock attribute we can simply reassign.
    cur.lastrowid = lastrowid
    return cur


def _db(conn: Any) -> Any:
    """Return a stand-in `Database` with just the `conn` attribute the orchestrator reads."""
    return cast("Any", SimpleNamespace(conn=conn))


def _user_record(uuid: str = "11111111-2222-3333-4444-555555555555") -> UserMessage:
    return UserMessage(
        uuid=uuid,
        parent_uuid=None,
        session_id="abcdef",
        timestamp="2026-04-29T00:00:00Z",
        blocks=(),
        text_content="hello",
        raw={"cwd": "/workspace/x", "gitBranch": "main"},
    )


def _patch_signature(mocker: MockerFixture, sha_hex: str) -> None:
    sha_bytes = bytes.fromhex(sha_hex)
    mocker.patch.object(
        orch,
        "compute_file_signature",
        autospec=True,
        return_value=(sha_bytes, _SIZE, _MTIME_NS),
    )


def _patch_not_empty(mocker: MockerFixture) -> None:
    mocker.patch.object(orch, "_is_empty_file", autospec=True, return_value=False)


def _executed_sql(conn: Any) -> list[str]:
    """Return the SQL string from each `conn.execute(sql, ...)` call, in order."""
    return [call.args[0] for call in conn.execute.call_args_list]


# --------------------------------------------------------------------------- #
# Scenario 1: hash match -> short-circuit, no transaction
# --------------------------------------------------------------------------- #


def test_short_circuits_when_hash_unchanged(mocker: MockerFixture) -> None:
    conn = _make_conn(mocker)
    _patch_not_empty(mocker)
    _patch_signature(mocker, _SHA_A)

    select_cur = _make_cursor(mocker, fetchone={"id": 7, "sha256": _SHA_A})
    conn.execute.return_value = select_cur

    parse_mock = mocker.patch.object(orch, "parse_file", autospec=True)

    result = index_file(_db(conn), _PATH)

    assert isinstance(result, IndexResult)
    assert result.skipped_reason == "unchanged"
    assert result.source_file_id == 7
    parse_mock.assert_not_called()
    # Only the SELECT lookup was issued; no BEGIN, no COMMIT, no ROLLBACK.
    sqls = _executed_sql(conn)
    assert len(sqls) == 1
    assert sqls[0].startswith("SELECT id, sha256 FROM source_files")
    assert not any("BEGIN EXCLUSIVE" in s for s in sqls)
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


# --------------------------------------------------------------------------- #
# Scenario 2: first-time index -> insert + commit, no delete
# --------------------------------------------------------------------------- #


def test_first_time_index_inserts_and_commits(mocker: MockerFixture) -> None:
    conn = _make_conn(mocker)
    _patch_not_empty(mocker)
    _patch_signature(mocker, _SHA_A)

    select_cur = _make_cursor(mocker, fetchone=None)  # not in source_files yet
    insert_source_cur = _make_cursor(mocker, lastrowid=42)
    other_cur = _make_cursor(mocker)

    def execute_side_effect(sql: str, *_args: Any, **_kwargs: Any) -> Any:
        if sql.startswith("SELECT id, sha256 FROM source_files"):
            return select_cur
        if sql.startswith("INSERT INTO source_files"):
            return insert_source_cur
        return other_cur

    conn.execute.side_effect = execute_side_effect

    parse_mock = mocker.patch.object(
        orch,
        "parse_file",
        autospec=True,
        return_value=iter([_user_record()]),
    )

    result = index_file(_db(conn), _PATH)

    parse_mock.assert_called_once_with(_PATH)
    assert isinstance(result, IndexResult)
    assert result.error is None
    assert result.skipped_reason is None
    assert result.source_file_id == 42

    sqls = _executed_sql(conn)
    assert any(s == "BEGIN EXCLUSIVE" for s in sqls)
    assert sum(1 for s in sqls if s == "BEGIN EXCLUSIVE") == 1
    # No DELETE because there was no existing row.
    assert not any(s.startswith("DELETE FROM source_files") for s in sqls)
    # INSERT into source_files used the path/size/mtime/sha and an iso timestamp.
    insert_call = next(
        c for c in conn.execute.call_args_list if c.args[0].startswith("INSERT INTO source_files")
    )
    params = insert_call.args[1]
    assert params[0] == str(_PATH)
    assert params[1] == _SIZE
    assert params[2] == _MTIME_NS
    assert params[3] == _SHA_A
    assert isinstance(params[4], str)
    assert params[4]  # iso timestamp non-empty

    # Two commits: one settles state before BEGIN EXCLUSIVE, one finalizes after persist.
    assert conn.commit.call_count == 2
    conn.rollback.assert_not_called()


# --------------------------------------------------------------------------- #
# Scenario 3: re-index with hash mismatch -> delete existing, then persist
# --------------------------------------------------------------------------- #


def test_reindex_on_hash_mismatch_deletes_existing(mocker: MockerFixture) -> None:
    conn = _make_conn(mocker)
    _patch_not_empty(mocker)
    _patch_signature(mocker, _SHA_B)  # current sha differs from stored

    select_cur = _make_cursor(mocker, fetchone={"id": 99, "sha256": _SHA_A})
    insert_source_cur = _make_cursor(mocker, lastrowid=100)
    other_cur = _make_cursor(mocker)

    def execute_side_effect(sql: str, *_args: Any, **_kwargs: Any) -> Any:
        if sql.startswith("SELECT id, sha256 FROM source_files"):
            return select_cur
        if sql.startswith("INSERT INTO source_files"):
            return insert_source_cur
        return other_cur

    conn.execute.side_effect = execute_side_effect

    mocker.patch.object(
        orch,
        "parse_file",
        autospec=True,
        return_value=iter([_user_record()]),
    )

    result = index_file(_db(conn), _PATH)

    sqls = _executed_sql(conn)
    # DELETE FROM source_files WHERE id = ? was issued with the stored id.
    delete_calls = [
        c for c in conn.execute.call_args_list if c.args[0].startswith("DELETE FROM source_files")
    ]
    assert len(delete_calls) == 1
    assert delete_calls[0].args[1] == (99,)

    # And BEGIN EXCLUSIVE still framed the transaction.
    assert any(s == "BEGIN EXCLUSIVE" for s in sqls)
    assert conn.commit.call_count == 2  # pre-BEGIN settle + post-persist finalize
    conn.rollback.assert_not_called()
    assert result.source_file_id == 100


# --------------------------------------------------------------------------- #
# Scenario 4: parser raises mid-file -> rollback, no commit, IndexResult.error set
# --------------------------------------------------------------------------- #


def test_parser_error_triggers_rollback(mocker: MockerFixture) -> None:
    conn = _make_conn(mocker)
    _patch_not_empty(mocker)
    _patch_signature(mocker, _SHA_A)

    select_cur = _make_cursor(mocker, fetchone=None)
    other_cur = _make_cursor(mocker)

    def execute_side_effect(sql: str, *_args: Any, **_kwargs: Any) -> Any:
        if sql.startswith("SELECT id, sha256 FROM source_files"):
            return select_cur
        return other_cur

    conn.execute.side_effect = execute_side_effect

    def boom(_path: Path) -> Any:
        raise IntakeParseError(line="garbage\n", lineno=3, reason="invalid json")

    mocker.patch.object(orch, "parse_file", autospec=True, side_effect=boom)

    result = index_file(_db(conn), _PATH)

    assert isinstance(result, IndexResult)
    assert result.error == "invalid json"
    assert result.error_at_line == 3
    assert result.source_file_id is None

    conn.rollback.assert_called_once()
    conn.commit.assert_called_once()  # the pre-BEGIN settle commit; but no post-persist commit
    # Specifically, no INSERT INTO source_files was issued — the row is NOT updated.
    sqls = _executed_sql(conn)
    assert not any(s.startswith("INSERT INTO source_files") for s in sqls)


# --------------------------------------------------------------------------- #
# Scenario 5: persistence (DB) raises mid-write -> rollback, no commit
# --------------------------------------------------------------------------- #


def test_persistence_database_error_triggers_rollback(mocker: MockerFixture) -> None:
    conn = _make_conn(mocker)
    _patch_not_empty(mocker)
    _patch_signature(mocker, _SHA_A)

    select_cur = _make_cursor(mocker, fetchone=None)
    insert_source_cur = _make_cursor(mocker, lastrowid=42)
    other_cur = _make_cursor(mocker)

    # Raise on the INSERT INTO sessions (the third execute after SELECT, BEGIN
    # EXCLUSIVE, PRAGMA, INSERT INTO source_files... actually the orchestrator
    # issues the session INSERT after the source_files INSERT). Targeting any
    # `INSERT OR IGNORE INTO sessions` call satisfies the "mid-write" intent.
    def execute_side_effect(sql: str, *_args: Any, **_kwargs: Any) -> Any:
        if sql.startswith("SELECT id, sha256 FROM source_files"):
            return select_cur
        if sql.startswith("INSERT INTO source_files"):
            return insert_source_cur
        if sql.startswith("INSERT OR IGNORE INTO sessions"):
            msg = "FOREIGN KEY constraint failed"
            raise sqlite3.IntegrityError(msg)
        return other_cur

    conn.execute.side_effect = execute_side_effect

    mocker.patch.object(
        orch,
        "parse_file",
        autospec=True,
        return_value=iter([_user_record()]),
    )

    result = index_file(_db(conn), _PATH)

    assert isinstance(result, IndexResult)
    assert result.error is not None
    assert "FOREIGN KEY" in result.error
    assert result.source_file_id is None

    conn.rollback.assert_called_once()
    # Only the pre-BEGIN settle commit fired; no post-persist commit.
    assert conn.commit.call_count == 1


# --------------------------------------------------------------------------- #
# Scenario 6: dry-run classification (tree-level) -> no writes
# --------------------------------------------------------------------------- #


def test_classify_dry_run_emits_no_writes_for_unchanged(mocker: MockerFixture) -> None:
    conn = _make_conn(mocker)
    _patch_not_empty(mocker)
    _patch_signature(mocker, _SHA_A)

    select_cur = _make_cursor(mocker, fetchone={"id": 7, "sha256": _SHA_A})
    conn.execute.return_value = select_cur

    parse_mock = mocker.patch.object(orch, "parse_file", autospec=True)

    result = orch._classify_dry_run(conn, _PATH, full=False)

    assert result.skipped_reason == "dry_run_unchanged"
    assert result.source_file_id == 7
    parse_mock.assert_not_called()
    sqls = _executed_sql(conn)
    # Only the SELECT lookup; no BEGIN/INSERT/UPDATE/DELETE.
    assert len(sqls) == 1
    assert sqls[0].startswith("SELECT id, sha256 FROM source_files")
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


def test_classify_dry_run_marks_new_file(mocker: MockerFixture) -> None:
    conn = _make_conn(mocker)
    _patch_not_empty(mocker)
    _patch_signature(mocker, _SHA_A)

    select_cur = _make_cursor(mocker, fetchone=None)
    conn.execute.return_value = select_cur

    result = orch._classify_dry_run(conn, _PATH, full=False)

    assert result.skipped_reason == "dry_run_new"
    assert result.source_file_id is None
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


# --------------------------------------------------------------------------- #
# Scenario 7: force=True re-indexes even when hash matches
# --------------------------------------------------------------------------- #


def test_force_reindexes_even_when_hash_matches(mocker: MockerFixture) -> None:
    conn = _make_conn(mocker)
    _patch_not_empty(mocker)
    _patch_signature(mocker, _SHA_A)

    # Stored sha matches current sha — without force this would short-circuit.
    select_cur = _make_cursor(mocker, fetchone={"id": 50, "sha256": _SHA_A})
    insert_source_cur = _make_cursor(mocker, lastrowid=51)
    other_cur = _make_cursor(mocker)

    def execute_side_effect(sql: str, *_args: Any, **_kwargs: Any) -> Any:
        if sql.startswith("SELECT id, sha256 FROM source_files"):
            return select_cur
        if sql.startswith("INSERT INTO source_files"):
            return insert_source_cur
        return other_cur

    conn.execute.side_effect = execute_side_effect

    parse_mock = mocker.patch.object(
        orch,
        "parse_file",
        autospec=True,
        return_value=iter([_user_record()]),
    )

    result = index_file(_db(conn), _PATH, force=True)

    parse_mock.assert_called_once_with(_PATH)
    sqls = _executed_sql(conn)
    assert any(s == "BEGIN EXCLUSIVE" for s in sqls)
    # The existing row is deleted before re-insert.
    assert any(s.startswith("DELETE FROM source_files") for s in sqls)
    # New source_files row inserted; commit fires; no rollback.
    assert any(s.startswith("INSERT INTO source_files") for s in sqls)
    assert conn.commit.call_count == 2  # pre-BEGIN settle + post-persist finalize
    conn.rollback.assert_not_called()
    assert result.source_file_id == 51
    assert result.skipped_reason is None


# --------------------------------------------------------------------------- #
# Bonus: empty-file short-circuit (covers `_SKIP_EMPTY` branch)
# --------------------------------------------------------------------------- #


def test_empty_file_short_circuits_with_skip_empty(mocker: MockerFixture) -> None:
    conn = _make_conn(mocker)
    mocker.patch.object(orch, "_is_empty_file", autospec=True, return_value=True)
    sig_mock = mocker.patch.object(orch, "compute_file_signature", autospec=True)
    parse_mock = mocker.patch.object(orch, "parse_file", autospec=True)

    result = index_file(_db(conn), _PATH)

    assert result.skipped_reason == "empty"
    assert result.source_file_id is None
    sig_mock.assert_not_called()
    parse_mock.assert_not_called()
    conn.execute.assert_not_called()
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


# --------------------------------------------------------------------------- #
# Guard: `index_file` raises if the database is closed.
# --------------------------------------------------------------------------- #


def test_index_file_raises_when_db_closed() -> None:
    db = cast("Any", SimpleNamespace(conn=None))
    with pytest.raises(RuntimeError, match="not open"):
        index_file(db, _PATH)
