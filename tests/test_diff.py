"""Unit tests for `compute_diff` analytics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from convo.analytics import compute_diff
from tests._seed import seed_message, seed_source_file

if TYPE_CHECKING:
    from convo.db import Database


def _ts(offset: timedelta) -> str:
    """ISO-8601 (with Z) for `now - offset`."""
    return (datetime.now(UTC) - offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_session(
    db: Database,
    *,
    sid: str,
    sfid: int,
    project: str,
    started_at: str,
    ended_at: str,
    model: str,
) -> None:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at, "
        "ended_at, model) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, sfid, project, started_at, ended_at, model),
    )
    db.conn.commit()


def _seed_tool_call(
    db: Database,
    *,
    tcid: str,
    mid: str,
    sid: str,
    name: str,
    started_at: str,
    duration_ms: int = 100,
) -> None:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO tool_calls(id, message_id, session_id, seq, name, "
        "input_json, started_at, duration_ms) VALUES (?, ?, ?, 0, ?, '{}', ?, ?)",
        (tcid, mid, sid, name, started_at, duration_ms),
    )
    db.conn.commit()


def _seed_message_with_ts(
    db: Database,
    *,
    sid: str,
    mid: str,
    content: str,
    timestamp: str,
) -> None:
    seed_message(db, sid, mid=mid, content=content)
    assert db.conn is not None
    db.conn.execute("UPDATE messages SET timestamp = ? WHERE id = ?", (timestamp, mid))
    db.conn.commit()


def _populate_two_windows(db: Database) -> None:
    """Seed rows into both the current (3d ago) and previous (10d ago) windows."""
    sf_cur = seed_source_file(db, path="/data/cur.jsonl")
    sf_prev = seed_source_file(db, path="/data/prev.jsonl")
    cur_ts = _ts(timedelta(days=3))
    prev_ts = _ts(timedelta(days=10))
    cur_end = _ts(timedelta(days=3, seconds=-30))
    prev_end = _ts(timedelta(days=10, seconds=-10))

    _seed_session(
        db,
        sid="sCur",
        sfid=sf_cur,
        project="/proj/A",
        started_at=cur_ts,
        ended_at=cur_end,
        model="opus-4",
    )
    _seed_session(
        db,
        sid="sPrev",
        sfid=sf_prev,
        project="/proj/A",
        started_at=prev_ts,
        ended_at=prev_end,
        model="sonnet-4",
    )
    _seed_message_with_ts(db, sid="sCur", mid="mC", content="run build", timestamp=cur_ts)
    _seed_message_with_ts(db, sid="sPrev", mid="mP", content="old build", timestamp=prev_ts)
    # Two Bash calls in current; one Read in previous.
    _seed_tool_call(db, tcid="tcC1", mid="mC", sid="sCur", name="Bash", started_at=cur_ts)
    _seed_tool_call(db, tcid="tcC2", mid="mC", sid="sCur", name="Bash", started_at=cur_ts)
    _seed_tool_call(db, tcid="tcP1", mid="mP", sid="sPrev", name="Read", started_at=prev_ts)


def test_compute_diff_default_span_is_7d(db: Database) -> None:
    _populate_two_windows(db)
    report = compute_diff(db)
    assert report.span_seconds == timedelta(days=7).total_seconds()


def test_compute_diff_partitions_rows_into_windows(db: Database) -> None:
    _populate_two_windows(db)
    report = compute_diff(db, span=timedelta(days=7))

    # current: 1 session, 2 tool_calls (Bash), 1 message
    assert report.current.sessions_count == 1
    assert report.current.tool_calls_total == 2
    assert report.current.tool_calls_by_name == {"Bash": 2}
    assert report.current.commands_total == 1
    assert report.current.commands_top == {"run build": 1}
    assert report.current.files_count == 1
    assert report.current.model_histogram == {"opus-4": 1}

    # previous: 1 session, 1 tool_call (Read)
    assert report.previous.sessions_count == 1
    assert report.previous.tool_calls_total == 1
    assert report.previous.tool_calls_by_name == {"Read": 1}
    assert report.previous.commands_total == 1
    assert report.previous.commands_top == {"old build": 1}
    assert report.previous.files_count == 1
    assert report.previous.model_histogram == {"sonnet-4": 1}


def test_compute_diff_deltas_match_window_diffs(db: Database) -> None:
    _populate_two_windows(db)
    report = compute_diff(db, span=timedelta(days=7))
    # tool_calls_total: 2 - 1 = +1, pct = 1.0
    assert report.deltas.tool_calls_total.absolute == 1
    assert report.deltas.tool_calls_total.pct == pytest.approx(1.0)
    # Both windows have one session each, so the count delta is zero.
    assert report.deltas.sessions_count.absolute == 0
    assert report.deltas.sessions_count.pct == pytest.approx(0.0)
    # tool_calls_by_name: Bash new (+2, pct=None), Read gone (-1, pct=-1.0)
    assert report.deltas.tool_calls_by_name["Bash"].absolute == 2
    assert report.deltas.tool_calls_by_name["Bash"].pct is None
    assert report.deltas.tool_calls_by_name["Read"].absolute == -1
    assert report.deltas.tool_calls_by_name["Read"].pct == pytest.approx(-1.0)


def test_compute_diff_all_new(db: Database) -> None:
    """previous=empty, current>0 → positive deltas with pct=None."""
    sfid = seed_source_file(db, path="/data/x.jsonl")
    cur_ts = _ts(timedelta(days=3))
    cur_end = _ts(timedelta(days=3, seconds=-10))
    _seed_session(
        db,
        sid="s1",
        sfid=sfid,
        project="/proj/A",
        started_at=cur_ts,
        ended_at=cur_end,
        model="opus-4",
    )
    _seed_message_with_ts(db, sid="s1", mid="m1", content="hi", timestamp=cur_ts)
    _seed_tool_call(db, tcid="tc1", mid="m1", sid="s1", name="Bash", started_at=cur_ts)

    report = compute_diff(db, span=timedelta(days=7))
    assert report.previous.tool_calls_total == 0
    assert report.previous.sessions_count == 0
    assert report.deltas.tool_calls_total.absolute == 1
    assert report.deltas.tool_calls_total.pct is None
    assert report.deltas.sessions_count.absolute == 1
    assert report.deltas.sessions_count.pct is None
    # The Bash bucket appears in the current window only; pct is None ("new").
    assert report.deltas.tool_calls_by_name["Bash"].pct is None


def test_compute_diff_all_gone(db: Database) -> None:
    """previous>0, current=empty → negative deltas with pct=-1.0."""
    sfid = seed_source_file(db, path="/data/x.jsonl")
    prev_ts = _ts(timedelta(days=10))
    prev_end = _ts(timedelta(days=10, seconds=-10))
    _seed_session(
        db,
        sid="s1",
        sfid=sfid,
        project="/proj/A",
        started_at=prev_ts,
        ended_at=prev_end,
        model="opus-4",
    )
    _seed_message_with_ts(db, sid="s1", mid="m1", content="hi", timestamp=prev_ts)
    _seed_tool_call(db, tcid="tc1", mid="m1", sid="s1", name="Bash", started_at=prev_ts)

    report = compute_diff(db, span=timedelta(days=7))
    assert report.current.tool_calls_total == 0
    assert report.current.sessions_count == 0
    assert report.deltas.tool_calls_total.absolute == -1
    assert report.deltas.tool_calls_total.pct == pytest.approx(-1.0)
    assert report.deltas.sessions_count.pct == pytest.approx(-1.0)
    assert report.deltas.tool_calls_by_name["Bash"].absolute == -1


def test_compute_diff_empty_db(db: Database) -> None:
    report = compute_diff(db, span=timedelta(days=7))
    assert report.current.tool_calls_total == 0
    assert report.previous.tool_calls_total == 0
    assert report.deltas.tool_calls_total.absolute == 0
    assert report.deltas.tool_calls_total.pct == pytest.approx(0.0)
    assert report.deltas.sessions_count.absolute == 0
    assert report.deltas.sessions_median_seconds.absolute == 0
    # Mappings should be empty (union of two empty sets)
    assert dict(report.deltas.tool_calls_by_name) == {}
    assert dict(report.deltas.commands_top) == {}
    assert dict(report.deltas.model_histogram) == {}


def test_compute_diff_project_filter_narrows_both_windows(db: Database) -> None:
    sf_a = seed_source_file(db, path="/data/a.jsonl")
    sf_b = seed_source_file(db, path="/data/b.jsonl")
    cur_ts = _ts(timedelta(days=3))
    prev_ts = _ts(timedelta(days=10))
    cur_end = _ts(timedelta(days=3, seconds=-10))
    prev_end = _ts(timedelta(days=10, seconds=-10))
    _seed_session(
        db,
        sid="sA",
        sfid=sf_a,
        project="/proj/A",
        started_at=cur_ts,
        ended_at=cur_end,
        model="opus-4",
    )
    _seed_session(
        db,
        sid="sB",
        sfid=sf_b,
        project="/proj/B",
        started_at=prev_ts,
        ended_at=prev_end,
        model="sonnet-4",
    )
    _seed_message_with_ts(db, sid="sA", mid="mA", content="A cmd", timestamp=cur_ts)
    _seed_message_with_ts(db, sid="sB", mid="mB", content="B cmd", timestamp=prev_ts)
    _seed_tool_call(db, tcid="tcA", mid="mA", sid="sA", name="Bash", started_at=cur_ts)
    _seed_tool_call(db, tcid="tcB", mid="mB", sid="sB", name="Read", started_at=prev_ts)

    report = compute_diff(db, span=timedelta(days=7), project="/proj/A")
    # Only /proj/A is visible; previous window for /proj/A is empty.
    assert report.current.sessions_count == 1
    assert report.previous.sessions_count == 0
    assert report.current.tool_calls_total == 1
    assert report.previous.tool_calls_total == 0
    assert "Read" not in report.current.tool_calls_by_name


def test_compute_diff_rejects_zero_span(db: Database) -> None:
    with pytest.raises(ValueError, match="span"):
        compute_diff(db, span=timedelta(seconds=0))
