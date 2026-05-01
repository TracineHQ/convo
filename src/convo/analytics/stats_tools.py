"""`convo stats tools` — tool-call frequency, duration, and error-rate report."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

from convo.read._db_access import open_ro
from convo.read.filters import since_iso

if TYPE_CHECKING:
    import sqlite3
    from datetime import timedelta

    from convo.db import Database


_TOP_FREQ_LIMIT: int = 20
_TOP_DURATION_LIMIT: int = 10
_MIN_DURATION_SAMPLES: int = 1


@dataclass(frozen=True, slots=True)
class ToolFreq:
    """Tool-call frequency row."""

    name: str
    count: int


@dataclass(frozen=True, slots=True)
class ToolDurationStat:
    """Median duration_ms for one tool (only tools with a recorded duration)."""

    name: str
    median_ms: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class ToolErrorRate:
    """Error rate per tool: errors / total. Total >= 1."""

    name: str
    total: int
    errors: int
    error_rate: float


@dataclass(frozen=True, slots=True)
class ToolsReport:
    """Aggregate tool-call statistics over a (since, project) window."""

    total: int
    top_by_frequency: tuple[ToolFreq, ...]
    top_by_median_duration: tuple[ToolDurationStat, ...]
    error_rates: tuple[ToolErrorRate, ...]


def stats_tools(
    db: Database,
    *,
    since: timedelta | None = None,
    project: str | None = None,
) -> ToolsReport:
    """Compute tool-call frequencies, median durations, and per-tool error rates."""
    cutoff = since_iso(since)
    ro = open_ro(db.path)
    try:
        total = _total_calls(ro, cutoff=cutoff, project=project)
        top_freq = _top_by_frequency(ro, cutoff=cutoff, project=project)
        top_dur = _top_by_median_duration(ro, cutoff=cutoff, project=project)
        errors = _error_rates(ro, cutoff=cutoff, project=project)
    finally:
        ro.close()
    return ToolsReport(
        total=total,
        top_by_frequency=top_freq,
        top_by_median_duration=top_dur,
        error_rates=errors,
    )


def _where_and_params(*, cutoff: str | None, project: str | None) -> tuple[str, list[object], bool]:
    """Build WHERE clause for tool_calls + (optional) sessions JOIN.

    Returns (where_sql, params, needs_session_join).
    `needs_session_join` is True iff project filter is set.
    """
    where: list[str] = []
    params: list[object] = []
    if cutoff is not None:
        where.append("tc.started_at IS NOT NULL AND tc.started_at >= ?")
        params.append(cutoff)
    if project is not None:
        where.append("s.project_path = ?")
        params.append(project)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params, project is not None


def _from_clause(*, needs_session_join: bool) -> str:
    base = "FROM tool_calls tc"
    if needs_session_join:
        return base + " JOIN sessions s ON s.id = tc.session_id"
    return base


def _total_calls(conn: sqlite3.Connection, *, cutoff: str | None, project: str | None) -> int:
    where_sql, params, needs_join = _where_and_params(cutoff=cutoff, project=project)
    sql = f"SELECT COUNT(*) {_from_clause(needs_session_join=needs_join)}{where_sql}"
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def _top_by_frequency(
    conn: sqlite3.Connection, *, cutoff: str | None, project: str | None
) -> tuple[ToolFreq, ...]:
    where_sql, params, needs_join = _where_and_params(cutoff=cutoff, project=project)
    sql = (
        f"SELECT tc.name AS name, COUNT(*) AS n "
        f"{_from_clause(needs_session_join=needs_join)}"
        f"{where_sql} "
        "GROUP BY tc.name "
        "ORDER BY n DESC, tc.name "
        "LIMIT ?"
    )
    params.append(_TOP_FREQ_LIMIT)
    rows = conn.execute(sql, params).fetchall()
    return tuple(ToolFreq(name=str(r["name"]), count=int(r["n"])) for r in rows)


def _top_by_median_duration(
    conn: sqlite3.Connection, *, cutoff: str | None, project: str | None
) -> tuple[ToolDurationStat, ...]:
    """Fetch durations grouped by tool name, compute median per tool in Python.

    SQLite has no native median; the tool-name cardinality is small (<<100), so
    pulling per-name durations into Python is cheap and gives an exact median.
    """
    base_where, params, needs_join = _where_and_params(cutoff=cutoff, project=project)
    duration_filter = "tc.duration_ms IS NOT NULL"
    if base_where:
        where_sql = base_where + f" AND {duration_filter}"
    else:
        where_sql = f" WHERE {duration_filter}"
    sql = (
        f"SELECT tc.name AS name, tc.duration_ms AS d "
        f"{_from_clause(needs_session_join=needs_join)}"
        f"{where_sql}"
    )
    rows = conn.execute(sql, params).fetchall()
    by_name: dict[str, list[int]] = {}
    for r in rows:
        by_name.setdefault(str(r["name"]), []).append(int(r["d"]))

    stats: list[ToolDurationStat] = []
    for name, durations in by_name.items():
        if len(durations) < _MIN_DURATION_SAMPLES:
            continue
        median = float(statistics.median(durations))
        stats.append(ToolDurationStat(name=name, median_ms=median, sample_count=len(durations)))
    # Sort descending by median; tie-break by name for stability.
    stats.sort(key=lambda s: (-s.median_ms, s.name))
    return tuple(stats[:_TOP_DURATION_LIMIT])


def _error_rates(
    conn: sqlite3.Connection, *, cutoff: str | None, project: str | None
) -> tuple[ToolErrorRate, ...]:
    where_sql, params, needs_join = _where_and_params(cutoff=cutoff, project=project)
    # LEFT JOIN tool_results so calls without a recorded result are still counted
    # in `total` with 0 errors.
    sql = (
        f"SELECT tc.name AS name, "
        f"COUNT(*) AS total, "
        f"SUM(COALESCE(tr.is_error, 0)) AS errors "
        f"{_from_clause(needs_session_join=needs_join)} "
        f"LEFT JOIN tool_results tr ON tr.tool_call_id = tc.id"
        f"{where_sql} "
        "GROUP BY tc.name "
        "ORDER BY tc.name"
    )
    rows = conn.execute(sql, params).fetchall()
    out: list[ToolErrorRate] = []
    for r in rows:
        total = int(r["total"])
        errors = int(r["errors"] or 0)
        rate = (errors / total) if total > 0 else 0.0
        out.append(ToolErrorRate(name=str(r["name"]), total=total, errors=errors, error_rate=rate))
    return tuple(out)
