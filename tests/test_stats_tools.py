"""Tests for `stats_tools` analytics family."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from convo.analytics import stats_tools
from tests._seed import seed_message, seed_source_file

if TYPE_CHECKING:
    from convo.db import Database


def _seed_calls(db: Database) -> None:
    """Seed: 3 sessions across 2 projects; 10+ tool_calls across 3 names; 1 errored."""
    sfid_a = seed_source_file(db, path="/data/a.jsonl")
    sfid_b = seed_source_file(db, path="/data/b.jsonl")
    assert db.conn is not None
    # session s1 on project A — older
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at) VALUES (?, ?, ?, ?)",
        ("s1", sfid_a, "/proj/A", "2020-01-01T00:00:00Z"),
    )
    # session s2 on project A — recent
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at) VALUES (?, ?, ?, ?)",
        ("s2", sfid_a, "/proj/A", "2999-01-01T00:00:00Z"),
    )
    # session s3 on project B — recent
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at) VALUES (?, ?, ?, ?)",
        ("s3", sfid_b, "/proj/B", "2999-01-01T00:00:00Z"),
    )
    db.conn.commit()
    seed_message(db, "s1", mid="m1")
    seed_message(db, "s2", mid="m2")
    seed_message(db, "s3", mid="m3")

    # 6 Bash on s1 (old), 3 Read on s2 (recent project A), 2 Edit on s3 (recent project B)
    rows: list[tuple[str, str, str, int, str, str | None, int | None]] = [
        *[(f"tc_b_{i}", "m1", "s1", i, "Bash", "2020-01-01T00:00:00Z", 100) for i in range(6)],
        *[(f"tc_r_{i}", "m2", "s2", i, "Read", "2999-01-01T00:00:00Z", 50) for i in range(3)],
        *[(f"tc_e_{i}", "m3", "s3", i, "Edit", "2999-01-01T00:00:00Z", None) for i in range(2)],
    ]
    for tcid, mid, sid, seq, name, ts, dur in rows:
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, "
            "input_json, started_at, duration_ms) VALUES (?, ?, ?, ?, ?, '{}', ?, ?)",
            (tcid, mid, sid, seq, name, ts, dur),
        )
    # one Bash result errored
    db.conn.execute(
        "INSERT INTO tool_results(tool_call_id, is_error, output_text) VALUES (?, 1, 'oops')",
        ("tc_b_0",),
    )
    # one Read result OK
    db.conn.execute(
        "INSERT INTO tool_results(tool_call_id, is_error, output_text) VALUES (?, 0, 'ok')",
        ("tc_r_0",),
    )
    db.conn.commit()


def test_stats_tools_total_and_top_frequency(db: Database) -> None:
    _seed_calls(db)
    report = stats_tools(db)
    assert report.total_calls == 11
    names_in_order = [f.name for f in report.top_by_frequency]
    assert names_in_order == ["Bash", "Read", "Edit"]
    counts = {f.name: f.count for f in report.top_by_frequency}
    assert counts == {"Bash": 6, "Read": 3, "Edit": 2}


def test_stats_tools_error_rate(db: Database) -> None:
    _seed_calls(db)
    report = stats_tools(db)
    rates = {er.name: er for er in report.error_rates}
    assert rates["Bash"].total == 6
    assert rates["Bash"].errors == 1
    assert abs(rates["Bash"].error_rate - (1.0 / 6.0)) < 1e-9
    assert rates["Read"].errors == 0
    assert rates["Read"].error_rate == 0.0
    assert rates["Edit"].total == 2
    assert rates["Edit"].errors == 0


def test_stats_tools_median_duration(db: Database) -> None:
    _seed_calls(db)
    report = stats_tools(db)
    medians = {s.name: s for s in report.top_by_median_duration}
    # Edit has no duration data → not present
    assert "Edit" not in medians
    assert medians["Bash"].median_ms == 100.0
    assert medians["Bash"].sample_count == 6
    assert medians["Read"].median_ms == 50.0


def test_stats_tools_since_filter(db: Database) -> None:
    _seed_calls(db)
    # Future cutoff drops the old s1 Bash calls.
    report = stats_tools(db, since=timedelta(days=1))
    # Only Read (3) + Edit (2) remain.
    assert report.total_calls == 5
    names = [f.name for f in report.top_by_frequency]
    assert "Bash" not in names


def test_stats_tools_project_filter(db: Database) -> None:
    _seed_calls(db)
    report = stats_tools(db, project="/proj/B")
    assert report.total_calls == 2
    names = [f.name for f in report.top_by_frequency]
    assert names == ["Edit"]


def test_stats_tools_empty_db(db: Database) -> None:
    report = stats_tools(db)
    assert report.total_calls == 0
    assert report.top_by_frequency == ()
    assert report.top_by_median_duration == ()
    assert report.error_rates == ()
