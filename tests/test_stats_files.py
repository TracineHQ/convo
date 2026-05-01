"""Tests for `stats_files` analytics family."""

from __future__ import annotations

from typing import TYPE_CHECKING

from convo.analytics import stats_files

if TYPE_CHECKING:
    from convo.db import Database


def _insert_source_file(
    db: Database,
    *,
    path: str,
    size: int,
    message_count: int,
) -> int:
    assert db.conn is not None
    cur = db.conn.execute(
        "INSERT INTO source_files(path, size, mtime_ns, last_indexed_at, message_count) "
        "VALUES (?, ?, 0, '2026-04-29T00:00:00Z', ?)",
        (path, size, message_count),
    )
    db.conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_stats_files_unfiltered_totals_and_top_n(db: Database) -> None:
    _insert_source_file(db, path="/a.jsonl", size=100, message_count=5)
    _insert_source_file(db, path="/b.jsonl", size=200, message_count=20)
    _insert_source_file(db, path="/c.jsonl", size=300, message_count=10)

    report = stats_files(db)
    assert report.total == 3
    assert report.total_size_bytes == 600
    assert report.total_message_count == 35
    # top order: b(20), c(10), a(5)
    paths_in_order = [f.path for f in report.top_files]
    assert paths_in_order == ["/b.jsonl", "/c.jsonl", "/a.jsonl"]
    assert report.top_files[0].message_count == 20
    assert report.top_files[0].size_bytes == 200


def test_stats_files_project_filter(db: Database) -> None:
    sfid_a = _insert_source_file(db, path="/a.jsonl", size=100, message_count=5)
    sfid_b = _insert_source_file(db, path="/b.jsonl", size=200, message_count=20)
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path) VALUES (?, ?, ?)",
        ("s1", sfid_a, "/proj/A"),
    )
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path) VALUES (?, ?, ?)",
        ("s2", sfid_b, "/proj/B"),
    )
    db.conn.commit()

    report = stats_files(db, project="/proj/A")
    assert report.total == 1
    assert report.total_size_bytes == 100
    assert report.total_message_count == 5
    assert [f.path for f in report.top_files] == ["/a.jsonl"]


def test_stats_files_empty_db(db: Database) -> None:
    report = stats_files(db)
    assert report.total == 0
    assert report.total_size_bytes == 0
    assert report.total_message_count == 0
    assert report.top_files == ()
