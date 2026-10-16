"""`convo stats tools` — tool-call frequency and error-rate report."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from convo.read._db_access import open_ro
from convo.read.filters import since_iso

if TYPE_CHECKING:
    import sqlite3
    from datetime import timedelta

    from convo.db import Database


_TOP_FREQ_LIMIT: int = 20


@dataclass(frozen=True, slots=True)
class ToolFreq:
    """Tool-call frequency row."""

    name: str
    count: int


@dataclass(frozen=True, slots=True)
class ToolDurationStat:
    """Median duration_ms for one tool. Never produced: intake records no durations."""

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
    """Always empty. The JSONL mapper stores no tool durations (duration_ms is
    NULL), so there is nothing to rank. Kept so v2 JSON consumers don't break;
    drop at the next envelope schema_version bump."""
    error_rates: tuple[ToolErrorRate, ...]


def stats_tools(
    db: Database,
    *,
    since: timedelta | None = None,
    project: str | None = None,
) -> ToolsReport:
    """Compute tool-call frequencies and per-tool error rates."""
    cutoff = since_iso(since)
    ro = open_ro(db.path)
    try:
        total = _total_calls(ro, cutoff=cutoff, project=project)
        top_freq = _top_by_frequency(ro, cutoff=cutoff, project=project)
        errors = _error_rates(ro, cutoff=cutoff, project=project)
    finally:
        ro.close()
    return ToolsReport(
        total=total,
        top_by_frequency=top_freq,
        top_by_median_duration=(),
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
