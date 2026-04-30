"""Tests for `convo.intake.orchestrator.index_tree`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from convo.db import Database
from convo.intake.orchestrator import IndexReport, IndexResult, index_tree

if TYPE_CHECKING:
    from pathlib import Path


def _user_record(uuid: str, sid: str, text: str) -> dict[str, object]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": sid,
        "timestamp": "2026-04-29T00:00:00Z",
        "cwd": "/tmp/proj",
        "gitBranch": "main",
        "message": {"content": text},
    }


def _assistant_record(uuid: str, sid: str) -> dict[str, object]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": sid,
        "timestamp": "2026-04-29T00:01:00Z",
        "cwd": "/tmp/proj",
        "gitBranch": "main",
        "requestId": "req_1",
        "message": {
            "id": f"msg_{uuid}",
            "model": "claude-haiku-4-5",
            "content": [
                {"type": "text", "text": "ok"},
                {
                    "type": "tool_use",
                    "id": f"toolu_{uuid}",
                    "name": "Bash",
                    "input": {"command": "ls"},
                },
            ],
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    sids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    ]
    _write_jsonl(
        root / "alpha" / f"{sids[0]}.jsonl",
        [_user_record("u1", sids[0], "hi"), _assistant_record("a1", sids[0])],
    )
    _write_jsonl(
        root / "beta" / f"{sids[1]}.jsonl",
        [_user_record("u2", sids[1], "hello")],
    )
    _write_jsonl(
        root / "gamma" / f"{sids[2]}.jsonl",
        [_assistant_record("a2", sids[2])],
    )
    return root


def test_index_tree_indexes_all_files(db: Database, projects_dir: Path) -> None:
    report = index_tree(db, projects_dir)
    assert isinstance(report, IndexReport)
    assert report.files_seen == 3
    assert report.files_indexed == 3
    assert report.files_failed == 0
    assert report.rows_inserted["messages"] == 4
    assert report.rows_inserted["tool_calls"] == 2

    assert db.conn is not None
    assert db.conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4


def test_index_tree_dry_run_writes_nothing(db: Database, projects_dir: Path) -> None:
    report = index_tree(db, projects_dir, dry_run=True)
    assert report.files_seen == 3
    assert report.files_indexed == 3
    assert db.conn is not None
    assert db.conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_index_tree_dry_run_after_index_classifies_unchanged(
    db: Database,
    projects_dir: Path,
) -> None:
    index_tree(db, projects_dir)
    report = index_tree(db, projects_dir, dry_run=True)
    assert report.files_indexed == 0
    assert report.files_skipped_unchanged == 3


def test_index_tree_full_reindexes(db: Database, projects_dir: Path) -> None:
    index_tree(db, projects_dir)
    second = index_tree(db, projects_dir, full=True)
    assert second.files_indexed == 3
    assert second.files_skipped_unchanged == 0
    assert db.conn is not None
    assert db.conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4


def test_index_tree_reports_corrupt_file(db: Database, projects_dir: Path) -> None:
    bad = projects_dir / "delta" / "44444444-4444-4444-4444-444444444444.jsonl"
    bad.parent.mkdir(parents=True)
    bad.write_text(
        json.dumps(_user_record("u1", "44444444-4444-4444-4444-444444444444", "ok"))
        + "\n"
        + '{"type":\n',
        encoding="utf-8",
    )

    report = index_tree(db, projects_dir)
    assert report.files_seen == 4
    assert report.files_indexed == 3
    assert report.files_failed == 1
    assert len(report.errors) == 1
    err_path, err_msg, err_line = report.errors[0]
    assert err_path == bad
    assert "Invalid JSON" in err_msg
    assert err_line == 2

    assert db.conn is not None
    assert db.conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 3


def test_index_tree_on_file_callback(db: Database, projects_dir: Path) -> None:
    seen: list[IndexResult] = []
    index_tree(db, projects_dir, on_file=seen.append)
    assert len(seen) == 3
    assert all(r.error is None for r in seen)


def test_index_tree_empty_dir(db: Database, tmp_path: Path) -> None:
    empty = tmp_path / "empty-projects"
    empty.mkdir()
    report = index_tree(db, empty)
    assert report.files_seen == 0
    assert report.files_indexed == 0
    assert report.files_failed == 0
    assert report.duration_ms >= 0


def test_index_tree_counts_unknown_record_types(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "projects"
    sid = "55555555-5555-5555-5555-555555555555"
    path = root / "epsilon" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        _user_record("u1", sid, "hi"),
        {"type": "alien", "sessionId": sid},
        {"type": "alien", "sessionId": sid},
        {"type": "future-thing", "sessionId": sid},
    ]
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n",
        encoding="utf-8",
    )
    report = index_tree(db, root)
    assert report.unknown_record_types == {"alien": 2, "future-thing": 1}


def test_index_tree_skips_empty_files(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "projects"
    empty = root / "alpha" / "66666666-6666-6666-6666-666666666666.jsonl"
    empty.parent.mkdir(parents=True)
    empty.write_bytes(b"")
    sid = "77777777-7777-7777-7777-777777777777"
    _write_jsonl(root / "beta" / f"{sid}.jsonl", [_user_record("u1", sid, "hi")])

    report = index_tree(db, root)
    assert report.files_seen == 2
    assert report.files_skipped_empty == 1
    assert report.files_indexed == 1


def test_index_tree_raises_when_db_not_open(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    with pytest.raises(RuntimeError, match="not open"):
        index_tree(db, tmp_path)
