"""Tests for `stats_sessions` analytics family."""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

from convo.analytics import stats_sessions
from tests._seed import seed_source_file

if TYPE_CHECKING:
    from convo.db import Database


def _insert_session(
    db: Database,
    sfid: int,
    *,
    sid: str,
    started_at: str | None,
    ended_at: str | None,
) -> None:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, started_at, ended_at) VALUES (?, ?, ?, ?)",
        (sid, sfid, started_at, ended_at),
    )
    db.conn.commit()


def test_stats_sessions_count_median_p95(db: Database) -> None:

    sfid = seed_source_file(db, path="/data/s.jsonl")
    # 5 sessions with durations 10s, 20s, 30s, 40s, 1000s
    cases = [
        ("s1", "2026-04-29T01:00:00Z", "2026-04-29T01:00:10Z"),
        ("s2", "2026-04-29T02:00:00Z", "2026-04-29T02:00:20Z"),
        ("s3", "2026-04-29T03:00:00Z", "2026-04-29T03:00:30Z"),
        ("s4", "2026-04-29T04:00:00Z", "2026-04-29T04:00:40Z"),
        ("s5", "2026-04-29T05:00:00Z", "2026-04-29T05:16:40Z"),  # 1000s
    ]
    for sid, st, en in cases:
        _insert_session(db, sfid, sid=sid, started_at=st, ended_at=en)

    report = stats_sessions(db)
    assert report.total_sessions == 5
    assert report.sessions_with_duration == 5
    # julianday-based duration has small float drift; allow tolerance.
    assert report.median_duration_s is not None
    assert abs(report.median_duration_s - 30.0) < 0.01
    # 2 <= n < 20 → real p95 via statistics.quantiles(method="exclusive"),
    # not the old max() fallback (which incorrectly reported the 100th pct).
    expected_p95 = statistics.quantiles([10.0, 20.0, 30.0, 40.0, 1000.0], n=20, method="exclusive")[
        18
    ]
    assert report.p95_duration_s is not None
    assert abs(report.p95_duration_s - expected_p95) < 0.5


def test_stats_sessions_hour_of_day(db: Database) -> None:
    sfid = seed_source_file(db, path="/data/h.jsonl")
    # Two sessions started at hour 03 UTC, one at hour 14.
    _insert_session(
        db, sfid, sid="s1", started_at="2026-04-29T03:00:00Z", ended_at="2026-04-29T03:00:10Z"
    )
    _insert_session(
        db, sfid, sid="s2", started_at="2026-04-29T03:30:00Z", ended_at="2026-04-29T03:30:10Z"
    )
    _insert_session(
        db, sfid, sid="s3", started_at="2026-04-29T14:00:00Z", ended_at="2026-04-29T14:00:10Z"
    )

    report = stats_sessions(db)
    assert len(report.hour_of_day) == 24
    assert report.hour_of_day[3] == 2
    assert report.hour_of_day[14] == 1
    assert report.hour_of_day[0] == 0


def test_stats_sessions_handles_null_timestamps(db: Database) -> None:
    sfid = seed_source_file(db, path="/data/n.jsonl")
    _insert_session(db, sfid, sid="s1", started_at=None, ended_at=None)
    _insert_session(
        db, sfid, sid="s2", started_at="2026-04-29T01:00:00Z", ended_at="2026-04-29T01:00:10Z"
    )

    report = stats_sessions(db)
    assert report.total_sessions == 2
    assert report.sessions_with_duration == 1
    assert report.median_duration_s is not None
    assert abs(report.median_duration_s - 10.0) < 0.01


def test_stats_sessions_empty_db(db: Database) -> None:
    report = stats_sessions(db)
    assert report.total_sessions == 0
    assert report.sessions_with_duration == 0
    assert report.median_duration_s is None
    assert report.p95_duration_s is None
    assert report.hour_of_day == tuple([0] * 24)
