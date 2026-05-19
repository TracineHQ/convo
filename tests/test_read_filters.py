"""Tests for `parse_span` in `convo.read.filters`."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from convo.read import filters as _filters_mod
from convo.read.filters import ProjectResolveError, parse_span, resolve_project_path, since_iso


def test_parse_span_days() -> None:
    assert parse_span("7d") == timedelta(days=7)


def test_parse_span_hours() -> None:
    assert parse_span("24h") == timedelta(hours=24)


def test_parse_span_minutes() -> None:
    assert parse_span("90m") == timedelta(minutes=90)


def test_parse_span_seconds() -> None:
    assert parse_span("30s") == timedelta(seconds=30)


def test_parse_span_one_unit() -> None:
    assert parse_span("1d") == timedelta(days=1)
    assert parse_span("1h") == timedelta(hours=1)
    assert parse_span("1m") == timedelta(minutes=1)
    assert parse_span("1s") == timedelta(seconds=1)


def test_parse_span_weeks() -> None:
    assert parse_span("1w") == timedelta(days=7)
    assert parse_span("2w") == timedelta(days=14)


def test_parse_span_years() -> None:
    # `y` is approximated as 365 days; documented in `parse_span`.
    assert parse_span("1y") == timedelta(days=365)
    assert parse_span("3y") == timedelta(days=3 * 365)


def test_parse_span_huge_value_rejected() -> None:
    """Magnitudes that overflow `datetime.now(UTC) - timedelta(...)` raise ValueError.

    Regression: `parse_span("999999999d")` previously returned a valid timedelta,
    but `datetime.now(UTC) - timedelta(days=999999999)` raises `OverflowError`
    inside `since_iso`. The fix is to bound the magnitude at parse time.
    """
    with pytest.raises(ValueError, match="--since out of range"):
        parse_span("999999999d")


def test_parse_span_year_above_cap_rejected() -> None:
    # 101 years x 365 = 36865 > 36500 cap.
    with pytest.raises(ValueError, match="--since out of range"):
        parse_span("101y")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "7",
        "7days",
        "d7",
        "7D",
        "7H",
        "-7d",
        "0d",
        "07d",
        "7.5d",
        " 7d",
        "7d ",
        "7dd",
        "7Y",
        "7W",
        "P7D",
    ],
)
def test_parse_span_invalid(value: str) -> None:
    with pytest.raises(ValueError, match="invalid --since span"):
        parse_span(value)


def test_parse_span_iso_date() -> None:
    span = parse_span("2026-04-01")
    now = datetime.now(UTC)
    cutoff = datetime(2026, 4, 1, tzinfo=UTC)
    expected = now - cutoff
    # Allow ±1s tolerance for test execution time
    assert abs((span - expected).total_seconds()) < 1.0


def test_parse_span_iso_datetime() -> None:
    span = parse_span("2026-04-01T12:00:00Z")
    now = datetime.now(UTC)
    cutoff = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    expected = now - cutoff
    assert abs((span - expected).total_seconds()) < 1.0


def test_parse_span_iso_in_future_returns_negative() -> None:
    # ISO dates in the future yield a negative span; downstream logic
    # treats that as "from now backward zero seconds".
    span = parse_span("2999-01-01")
    assert span.total_seconds() < 0


def test_since_iso_none_returns_none() -> None:
    assert since_iso(None) is None


def test_since_iso_includes_fractional_seconds_for_lex_compare() -> None:
    """Cutoff must contain ``.`` so SQLite TEXT compare orders ``.000Z`` < ``.123Z``.

    Regression: with strftime("%Y-%m-%dT%H:%M:%SZ"), a record stored at
    ``2024-01-01T12:00:00.500Z`` would be silently excluded by ``--since`` when
    the computed cutoff was ``2024-01-01T12:00:00Z`` (because lex-compare puts
    ``.500Z`` < ``Z``). The fix: format cutoff with microseconds.
    """
    fixed_now = datetime(2024, 1, 1, 12, 0, 1, tzinfo=UTC)
    with patch.object(_filters_mod, "datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        cutoff = since_iso(timedelta(seconds=1, microseconds=500_000))

    assert cutoff is not None
    # The cutoff string must contain a `.` so SQLite TEXT compare matches
    # millisecond-precision stored timestamps.
    assert "." in cutoff, f"cutoff missing fractional component: {cutoff!r}"
    # The record at 12:00:00.500Z should compare >= cutoff (12:00:00.000000Z).
    record_ts = "2024-01-01T12:00:00.500Z"
    assert record_ts >= cutoff, (
        f"record {record_ts!r} should be included (>= cutoff {cutoff!r}) "
        "but lex-compare excludes it"
    )


def _make_projects_db(tmp_path) -> sqlite3.Connection:
    """Create a test DB with sample sessions."""
    db = tmp_path / "projects.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE sessions (id TEXT, project_path TEXT)")
    rows = [
        ("s1", "/Users/dev/develop/tracine-ops"),
        ("s2", "/Users/dev/develop/convo"),
        ("s3", "/Users/dev/develop/uu-rolecapacity-bff"),
        ("s4", "/Users/dev/develop/ai-toolkit"),
    ]
    conn.executemany("INSERT INTO sessions VALUES (?, ?)", rows)
    conn.commit()
    return conn


def test_resolve_exact_path(tmp_path) -> None:
    conn = _make_projects_db(tmp_path)
    assert resolve_project_path(conn, "/Users/dev/develop/tracine-ops") == (
        "/Users/dev/develop/tracine-ops"
    )


def test_resolve_basename(tmp_path) -> None:
    conn = _make_projects_db(tmp_path)
    assert resolve_project_path(conn, "tracine-ops") == "/Users/dev/develop/tracine-ops"


def test_resolve_substring_unambiguous(tmp_path) -> None:
    conn = _make_projects_db(tmp_path)
    assert resolve_project_path(conn, "ai-toolkit") == "/Users/dev/develop/ai-toolkit"


def test_resolve_ambiguous_raises_with_candidates(tmp_path) -> None:
    conn = _make_projects_db(tmp_path)
    with pytest.raises(ProjectResolveError) as exc_info:
        resolve_project_path(conn, "develop")
    msg = str(exc_info.value)
    assert "develop" in msg
    assert "tracine-ops" in msg
    assert "convo" in msg


def test_resolve_no_match_raises(tmp_path) -> None:
    conn = _make_projects_db(tmp_path)
    with pytest.raises(ProjectResolveError):
        resolve_project_path(conn, "nonexistent-project")
