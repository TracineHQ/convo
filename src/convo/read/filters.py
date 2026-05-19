"""Shared filter parsing helpers for read-side commands."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_SPAN_RE = re.compile(r"^(?P<n>[1-9][0-9]*)(?P<unit>[dhmswy])$")
# Each unit converts the integer to a number of days; we then build a
# `timedelta(days=...)` and bound the total against `_MAX_DAYS`. Using days
# uniformly keeps the overflow check trivial regardless of unit.
_UNIT_TO_DAYS: dict[str, float] = {
    "d": 1.0,
    "h": 1.0 / 24.0,
    "m": 1.0 / (24.0 * 60.0),
    "s": 1.0 / (24.0 * 60.0 * 60.0),
    "w": 7.0,
    # `y` is approximated as 365 days. Documented; leap years not corrected.
    "y": 365.0,
}

# Cap to 100 years per unit. `datetime.now(UTC) - timedelta(days=N)` overflows
# Python's `datetime.MINYEAR` for very large N; this bound is well within range
# and large enough that no realistic `--since` query bumps into it.
_MAX_DAYS: int = 36500

_ERR_INVALID_SPAN = (
    "invalid --since span: {value!r} (expected shorthand like 7d/24h/90m/30s/2w/1y "
    "or ISO date/datetime like 2026-04-01 or 2026-04-01T12:00:00Z)"
)
_ERR_OUT_OF_RANGE = "--since out of range; max is 36500 days"


def parse_span(s: str) -> timedelta:
    """Parse a span string into a timedelta.

    Accepted forms:
      - Shorthand: ``7d``, ``24h``, ``90m``, ``30s``, ``2w``, ``1y``
      - ISO date: ``2026-04-01``
      - ISO datetime: ``2026-04-01T12:00:00Z`` or ``2026-04-01T12:00:00+00:00``

    Returns the timedelta from the parsed instant until "now" (UTC). For ISO
    dates in the future, the returned span is negative; callers may interpret
    that as a zero-width window.
    """
    match = _SPAN_RE.match(s)
    if match is not None:
        n = int(match.group("n"))
        unit = match.group("unit")
        days = n * _UNIT_TO_DAYS[unit]
        if days > _MAX_DAYS:
            raise ValueError(_ERR_OUT_OF_RANGE)
        return timedelta(days=days)

    # Try ISO. Python 3.12+ fromisoformat accepts the trailing Z.
    try:
        parsed = datetime.fromisoformat(s.strip())
    except ValueError as exc:
        raise ValueError(_ERR_INVALID_SPAN.format(value=s)) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return datetime.now(UTC) - parsed


def since_iso(since: timedelta | None) -> str | None:
    """Format `since` as an ISO timestamp suitable for WHERE clauses.

    Stored timestamps are ISO-8601 with `Z` suffix and carry fractional seconds
    (e.g. ``2024-04-30T14:23:45.123Z``). SQLite compares TEXT lexicographically;
    ASCII ``.`` (0x2E) is less than ``Z`` (0x5A), so a cutoff formatted without
    a fractional component (``...:45Z``) sorts AFTER any same-second timestamp
    that does carry one (``...:45.123Z``), silently excluding rows on the
    cutoff second from ``--since`` windows. We therefore include a ``.``
    followed by microseconds in the cutoff so lexicographic ordering matches
    real-world stored values.
    """
    if since is None:
        return None
    cutoff = datetime.now(UTC) - since
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
