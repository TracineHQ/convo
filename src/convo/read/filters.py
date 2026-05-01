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
    "invalid --since span: {value!r} (expected one of: <N>d, <N>h, <N>m, <N>s, "
    "<N>w, <N>y with N >= 1, e.g. 7d, 24h, 90m, 30s, 2w, 1y)"
)
_ERR_OUT_OF_RANGE = "--since out of range; max is 36500 days"


def parse_span(s: str) -> timedelta:
    """Parse a shorthand duration like ``7d``, ``24h``, ``90m``, ``30s``, ``2w``, ``1y``.

    Accepts a positive integer followed by a single unit character: ``d`` (days),
    ``h`` (hours), ``m`` (minutes), ``s`` (seconds), ``w`` (weeks), ``y`` (years,
    approximated as 365 days). Rejects every other shape (zero/negative values,
    decimals, ISO durations, missing unit, trailing whitespace) with ``ValueError``.

    Magnitudes that translate to more than 36500 days (100 years) are rejected
    with ``ValueError`` to avoid downstream `datetime` overflow when the span is
    subtracted from "now".
    """
    match = _SPAN_RE.match(s)
    if match is None:
        raise ValueError(_ERR_INVALID_SPAN.format(value=s))
    n = int(match.group("n"))
    unit = match.group("unit")
    days = n * _UNIT_TO_DAYS[unit]
    if days > _MAX_DAYS:
        raise ValueError(_ERR_OUT_OF_RANGE)
    return timedelta(days=days)


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
