"""Shared filter parsing helpers for read-side commands."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_SPAN_RE = re.compile(r"^(?P<n>[1-9][0-9]*)(?P<unit>[dhms])$")
_UNIT_TO_KW: dict[str, str] = {
    "d": "days",
    "h": "hours",
    "m": "minutes",
    "s": "seconds",
}

_ERR_INVALID_SPAN = (
    "invalid --since span: {value!r} (expected one of: <N>d, <N>h, <N>m, <N>s "
    "with N >= 1, e.g. 7d, 24h, 90m, 30s)"
)


def parse_span(s: str) -> timedelta:
    """Parse a shorthand duration like ``7d``, ``24h``, ``90m``, ``30s``.

    Accepts a positive integer followed by a single unit character: ``d`` (days),
    ``h`` (hours), ``m`` (minutes), ``s`` (seconds). Rejects every other shape
    (zero/negative values, decimals, ISO durations, missing unit, trailing
    whitespace) with ``ValueError``.
    """
    match = _SPAN_RE.match(s)
    if match is None:
        raise ValueError(_ERR_INVALID_SPAN.format(value=s))
    n = int(match.group("n"))
    unit = match.group("unit")
    return timedelta(**{_UNIT_TO_KW[unit]: n})


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
