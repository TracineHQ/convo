"""Tests for the guard JSONL intake module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from convo.intake.guard import (
    GuardDecision,
    QuarantinedRecord,
    index_guard_file,
    parse_guard_file,
    resolve_guard_log_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from convo.db import Database


def _record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "v": 1,
        "schema_version": 1,
        "mode": "enforce",
        "timestamp": "2026-05-01T12:00:00.000000Z",
        "hook_id": "guard.bash_command_validator",
        "event": "PreToolUse",
        "tool_name": "Bash",
        "decision": "deny",
        "reason": "rm -rf is denied",
        "command_excerpt": "rm -rf /",
        "session_id": "sess-1",
        "cwd": "/home/alice/project",
    }
    base.update(overrides)
    return base


def _write_log(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_parse_valid_file_yields_decisions(tmp_path: Path) -> None:
    log = tmp_path / "guard.jsonl"
    _write_log(log, [_record(session_id=f"s{i}") for i in range(5)])
    records = list(parse_guard_file(log))
    assert len(records) == 5
    assert all(isinstance(r, GuardDecision) for r in records)
    assert [r.session_id for r in records if isinstance(r, GuardDecision)] == [
        "s0",
        "s1",
        "s2",
        "s3",
        "s4",
    ]


def test_parse_unsupported_v_quarantines(tmp_path: Path) -> None:
    log = tmp_path / "guard.jsonl"
    _write_log(log, [_record(), _record(v=99), _record()])
    records = list(parse_guard_file(log))
    assert len(records) == 3
    assert isinstance(records[0], GuardDecision)
    assert isinstance(records[1], QuarantinedRecord)
    assert "unsupported_v" in records[1].reason
    assert isinstance(records[2], GuardDecision)


def test_parse_invalid_json_quarantines(tmp_path: Path) -> None:
    log = tmp_path / "guard.jsonl"
    log.write_text("not json at all\n", encoding="utf-8")
    records = list(parse_guard_file(log))
    assert len(records) == 1
    assert isinstance(records[0], QuarantinedRecord)
    assert "invalid_json" in records[0].reason


def test_parse_missing_required_field_quarantines(tmp_path: Path) -> None:
    log = tmp_path / "guard.jsonl"
    rec = _record()
    del rec["reason"]
    _write_log(log, [rec])
    records = list(parse_guard_file(log))
    assert len(records) == 1
    assert isinstance(records[0], QuarantinedRecord)
    assert "missing_fields" in records[0].reason


def test_parse_unknown_decision_quarantines(tmp_path: Path) -> None:
    log = tmp_path / "guard.jsonl"
    _write_log(log, [_record(decision="explode")])
    records = list(parse_guard_file(log))
    assert len(records) == 1
    assert isinstance(records[0], QuarantinedRecord)
    assert "unknown_decision" in records[0].reason


def test_parse_blank_lines_skipped(tmp_path: Path) -> None:
    log = tmp_path / "guard.jsonl"
    log.write_text(
        json.dumps(_record()) + "\n\n  \n" + json.dumps(_record()) + "\n",
        encoding="utf-8",
    )
    records = list(parse_guard_file(log))
    assert len(records) == 2
    assert all(isinstance(r, GuardDecision) for r in records)


def test_index_inserts_rows(tmp_path: Path, db: Database) -> None:
    log = tmp_path / "guard.jsonl"
    _write_log(log, [_record(session_id=f"s{i}") for i in range(3)])
    result = index_guard_file(db, log)
    assert result.error is None
    assert result.inserted_rows.get("guard_decisions") == 3
    assert db.conn is not None
    count = db.conn.execute("SELECT COUNT(*) FROM guard_decisions").fetchone()[0]
    assert count == 3


def test_index_idempotent_on_unchanged_file(tmp_path: Path, db: Database) -> None:
    log = tmp_path / "guard.jsonl"
    _write_log(log, [_record()])
    first = index_guard_file(db, log)
    assert first.inserted_rows.get("guard_decisions") == 1
    second = index_guard_file(db, log)
    assert second.skipped_reason == "unchanged"
    assert db.conn is not None
    count = db.conn.execute("SELECT COUNT(*) FROM guard_decisions").fetchone()[0]
    assert count == 1


def test_index_force_reingests(tmp_path: Path, db: Database) -> None:
    log = tmp_path / "guard.jsonl"
    _write_log(log, [_record()])
    index_guard_file(db, log)
    # Modify file content; sha will change so non-force would also reingest.
    # Force path is what we're testing — verify count stays correct.
    _write_log(log, [_record(), _record(session_id="s2")])
    result = index_guard_file(db, log, force=True)
    assert result.inserted_rows.get("guard_decisions") == 2
    assert db.conn is not None
    count = db.conn.execute("SELECT COUNT(*) FROM guard_decisions").fetchone()[0]
    assert count == 2


def test_index_quarantined_count_in_result(tmp_path: Path, db: Database) -> None:
    log = tmp_path / "guard.jsonl"
    _write_log(log, [_record(), _record(v=99), _record()])
    result = index_guard_file(db, log)
    assert result.inserted_rows.get("guard_decisions") == 2
    assert result.inserted_rows.get("quarantined") == 1


def test_index_empty_file_skipped(tmp_path: Path, db: Database) -> None:
    log = tmp_path / "guard.jsonl"
    log.write_text("", encoding="utf-8")
    result = index_guard_file(db, log)
    assert result.skipped_reason == "empty"


def test_index_records_correct_kind(tmp_path: Path, db: Database) -> None:
    log = tmp_path / "guard.jsonl"
    _write_log(log, [_record()])
    index_guard_file(db, log)
    assert db.conn is not None
    kind = db.conn.execute(
        "SELECT kind FROM source_files WHERE path = ?",
        (str(log),),
    ).fetchone()[0]
    assert kind == "guard_decisions"


def test_index_fts_populated(tmp_path: Path, db: Database) -> None:
    log = tmp_path / "guard.jsonl"
    _write_log(log, [_record(reason="dangerous rm command")])
    index_guard_file(db, log)
    assert db.conn is not None
    hits = db.conn.execute(
        "SELECT reason FROM guard_decisions_fts WHERE guard_decisions_fts MATCH ?",
        ("dangerous",),
    ).fetchall()
    assert len(hits) == 1


def test_resolve_path_explicit(tmp_path: Path) -> None:
    log = tmp_path / "guard.jsonl"
    log.write_text("", encoding="utf-8")
    resolved = resolve_guard_log_path(log)
    assert resolved == log


def test_resolve_path_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "guard.jsonl"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("GUARD_DECISIONS_PATH", str(log))
    assert resolve_guard_log_path() == log


def test_resolve_path_missing_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUARD_DECISIONS_PATH", str(tmp_path / "nope.jsonl"))
    assert resolve_guard_log_path() is None


def test_resolve_path_redirect_pointer(tmp_path: Path) -> None:
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    pointer = tmp_path / "pointer.jsonl"
    pointer.write_text(json.dumps({"redirect": str(real)}) + "\n", encoding="utf-8")
    assert resolve_guard_log_path(pointer) == real


def test_resolve_path_redirect_target_missing(tmp_path: Path) -> None:
    pointer = tmp_path / "pointer.jsonl"
    pointer.write_text(
        json.dumps({"redirect": str(tmp_path / "vanished.jsonl")}) + "\n",
        encoding="utf-8",
    )
    assert resolve_guard_log_path(pointer) is None
