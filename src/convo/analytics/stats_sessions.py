"""`convo stats sessions` — count, median/p95 duration, hour-of-day histogram."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from convo.analytics._constants import SECONDS_PER_DAY
from convo.read._db_access import open_ro
from convo.read.filters import since_iso

if TYPE_CHECKING:
    from datetime import timedelta

    from convo.db import Database


_HOURS_IN_DAY: int = 24
_P95_QUANTILE_INDEX: int = 18  # quantiles(n=20) → 19 cutpoints; index 18 == 95th pct.
_QUANTILE_BUCKETS: int = 20
_MIN_QUANTILE_SAMPLES: int = 2  # statistics.quantiles requires n >= 2.


@dataclass(frozen=True, slots=True)
class SessionsReport:
    """Aggregate session-duration and hour-of-day distribution."""

    total: int
    sessions_with_duration: int
    median_duration_s: float | None
    p95_duration_s: float | None
    hour_of_day: tuple[int, ...]  # length 24, hour_of_day[h] = sessions started at hour h


def stats_sessions(
    db: Database,
    *,
    since: timedelta | None = None,
    project: str | None = None,
) -> SessionsReport:
    """Compute session count, median/p95 wallclock duration, and started-at histogram."""
    cutoff = since_iso(since)
    where: list[str] = []
    params: list[object] = []
    if cutoff is not None:
        where.append("started_at IS NOT NULL AND started_at >= ?")
        params.append(cutoff)
    if project is not None:
        where.append("project_path = ?")
        params.append(project)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    base_select = (
        "SELECT started_at, "
        "(julianday(ended_at) - julianday(started_at)) * ? AS duration_s "
        "FROM sessions"
    )
    sql = base_select + where_sql
    bind_params: list[object] = [SECONDS_PER_DAY, *params]

    ro = open_ro(db.path)
    try:
        rows = ro.execute(sql, bind_params).fetchall()
    finally:
        ro.close()

    total = len(rows)
    durations: list[float] = []
    hours = [0] * _HOURS_IN_DAY
    for r in rows:
        d = r["duration_s"]
        if d is not None:
            durations.append(float(d))
        ts = r["started_at"]
        if ts is not None:
            hour = _hour_of(str(ts))
            if hour is not None:
                hours[hour] += 1

    median = float(statistics.median(durations)) if durations else None
    p95: float | None = _p95(durations)
    return SessionsReport(
        total=total,
        sessions_with_duration=len(durations),
        median_duration_s=median,
        p95_duration_s=p95,
        hour_of_day=tuple(hours),
    )


def _hour_of(ts: str) -> int | None:
    """Parse an ISO-8601 timestamp; return its UTC hour (0..23) or None on failure."""
    # Stored timestamps are typically ISO with `Z`; normalize for fromisoformat.
    raw = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).hour
    except ValueError:
        return None


def _p95(durations: list[float]) -> float | None:
    """Compute the 95th percentile via `statistics.quantiles(n=20)` (index 18).

    Returns ``None`` for fewer than 2 samples (``statistics.quantiles`` rejects
    n<2). For 2..19 samples, uses the exclusive method, which works for any
    n>=2; for n>=20 it falls through to the same call.
    """
    if len(durations) < _MIN_QUANTILE_SAMPLES:
        return None
    cuts = statistics.quantiles(durations, n=_QUANTILE_BUCKETS, method="exclusive")
    return float(cuts[_P95_QUANTILE_INDEX])
