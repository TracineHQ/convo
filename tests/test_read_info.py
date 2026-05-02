"""Tests for `gather_info` in `convo.read.info`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from convo.db import Database
from convo.read.info import InfoReport, ProjectCount, gather_info
from tests._seed import (
    seed_message,
    seed_session,
    seed_source_file,
    seed_tool_call,
    seed_tool_result,
)

if TYPE_CHECKING:
    from pathlib import Path


def _seed_multi_project(db: Database) -> None:
    """Seed: 2 sessions on project A, 1 on B, 1 with NULL project."""
    sfid_a1 = seed_source_file(db, path="/data/a1.jsonl")
    sfid_a2 = seed_source_file(db, path="/data/a2.jsonl")
    sfid_b = seed_source_file(db, path="/data/b.jsonl")
    sfid_n = seed_source_file(db, path="/data/n.jsonl")
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path) VALUES (?, ?, ?)",
        ("sa1", sfid_a1, "/proj/A"),
    )
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path) VALUES (?, ?, ?)",
        ("sa2", sfid_a2, "/proj/A"),
    )
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path) VALUES (?, ?, ?)",
        ("sb", sfid_b, "/proj/B"),
    )
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path) VALUES (?, ?, ?)",
        ("sn", sfid_n, None),
    )
    # Add a couple of messages + a tool call/result to test row counts > 0.
    seed_message(db, "sa1", mid="m1", content="hello")
    seed_message(db, "sa1", mid="m2", content="world", seq=1)
    seed_message(db, "sb", mid="m3", content="other")
    tcid = seed_tool_call(db, "m1", "sa1", tcid="tc1", name="Bash")
    seed_tool_result(db, tcid, output_text="ok")
    db.conn.commit()


def test_gather_info_reports_all_fields(db: Database) -> None:
    _seed_multi_project(db)
    snapshot_dir = db.path.parent / "convo-backups"
    snapshot_dir.mkdir()
    snap1 = snapshot_dir / "convo-20260101-000000-000000.db"
    snap1.write_bytes(b"x" * 100)
    snap2 = snapshot_dir / "convo-20260102-000000-000000.db"
    snap2.write_bytes(b"y" * 250)
    # Files that should NOT be counted as snapshots:
    (snapshot_dir / "README.txt").write_text("ignore me")
    (snapshot_dir / "convo-other.txt").write_text("ignore me too")

    report = gather_info(db)

    assert isinstance(report, InfoReport)
    assert report.schema_version == 2
    assert report.row_counts == {
        "source_files": 4,
        "sessions": 4,
        "messages": 3,
        "tool_calls": 1,
        "tool_results": 1,
    }
    assert report.last_indexed_at is not None
    assert "2026" in report.last_indexed_at

    # Top projects: A=2, B=1, NULL=1. Order is A first, then B/NULL ties broken
    # arbitrarily by SQLite — but A must lead.
    assert len(report.top_projects) == 3
    assert report.top_projects[0] == ProjectCount(project_path="/proj/A", session_count=2)
    others = {p.project_path: p.session_count for p in report.top_projects[1:]}
    assert others == {"/proj/B": 1, None: 1}

    assert report.db_size_bytes > 0
    assert report.db_size_bytes == db.path.stat().st_size
    assert report.snapshot_dir_path == snapshot_dir
    assert report.snapshot_count == 2
    assert report.snapshot_total_bytes == 350


def test_gather_info_no_snapshot_dir(db: Database) -> None:
    seed_source_file(db, path="/data/x.jsonl")
    report = gather_info(db)
    assert report.snapshot_count == 0
    assert report.snapshot_total_bytes == 0
    # Path is reported regardless of whether the dir exists yet.
    assert report.snapshot_dir_path == db.path.parent / "convo-backups"


def test_gather_info_empty_db(db: Database) -> None:
    report = gather_info(db)
    assert report.row_counts == {
        "source_files": 0,
        "sessions": 0,
        "messages": 0,
        "tool_calls": 0,
        "tool_results": 0,
    }
    assert report.last_indexed_at is None
    assert report.top_projects == []
    assert report.snapshot_count == 0


def test_gather_info_top_projects_limit_5(db: Database) -> None:
    # Seed 7 distinct projects to ensure LIMIT 5 is enforced.
    for i in range(7):
        sfid = seed_source_file(db, path=f"/data/p{i}.jsonl")
        sid = f"s{i}"
        seed_session(db, sfid, sid=sid)
        assert db.conn is not None
        db.conn.execute(
            "UPDATE sessions SET project_path = ? WHERE id = ?",
            (f"/proj/{i}", sid),
        )
        db.conn.commit()

    report = gather_info(db)
    assert len(report.top_projects) == 5


def test_gather_info_raises_when_db_closed(db_path: Path) -> None:
    db = Database(db_path)
    db.open()
    db.close()
    with pytest.raises(RuntimeError, match="not open"):
        gather_info(db)
