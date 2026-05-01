"""Tests for `parse_span` in `convo.read.filters`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from convo.read import filters as _filters_mod
from convo.read.filters import parse_span, since_iso


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
