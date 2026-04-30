"""Tests for `parse_span` in `convo.read.filters`."""

from __future__ import annotations

from datetime import timedelta

import pytest

from convo.read.filters import parse_span


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
        "7y",
        "P7D",
    ],
)
def test_parse_span_invalid(value: str) -> None:
    with pytest.raises(ValueError, match="invalid --since span"):
        parse_span(value)
