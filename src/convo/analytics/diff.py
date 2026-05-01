"""`convo diff` — current-vs-previous window comparison.

Runs the same aggregations as the `stats_*` families over two consecutive
[lower, upper) windows of equal length and reports per-bucket deltas.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from convo.analytics._constants import SECONDS_PER_DAY
from convo.read._db_access import open_ro

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from convo.db import Database


_TOOLS_TOP_LIMIT: int = 20
_COMMANDS_TOP_LIMIT: int = 10
_TRUNC_LEN: int = 80
_QUANTILE_BUCKETS: int = 20
_P95_QUANTILE_INDEX: int = 18
_MIN_QUANTILE_SAMPLES: int = 2  # statistics.quantiles requires n >= 2.

_DEFAULT_SPAN: timedelta = timedelta(days=7)


def _format_iso(ts: datetime) -> str:
    # See `convo.read.filters.since_iso` — fractional component required so
    # SQLite's lexicographic TEXT compare matches real stored timestamps that
    # carry milliseconds (cutoff with ``.`` sorts before same-second values
    # that contain ``.123Z``).
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    """Subset of stats metrics for one [lower, upper) window."""

    lower: str
    upper: str
    tool_calls_total: int
    tool_calls_by_name: dict[str, int]
    commands_total: int
    commands_top: dict[str, int]
    sessions_count: int
    sessions_median_seconds: float | None
    sessions_p95_seconds: float | None
    files_count: int
    model_histogram: dict[str, int]


@dataclass(frozen=True, slots=True)
class Delta:
    """Per-bucket delta. `pct` is None when previous is 0 (no baseline)."""

    absolute: float
    pct: float | None


@dataclass(frozen=True, slots=True)
class DeltaReport:
    """Deltas across every metric in `WindowSnapshot`. Mappings union both windows."""

    tool_calls_total: Delta
    tool_calls_by_name: dict[str, Delta]
    commands_total: Delta
    commands_top: dict[str, Delta]
    sessions_count: Delta
    sessions_median_seconds: Delta
    sessions_p95_seconds: Delta
    files_count: Delta
    model_histogram: dict[str, Delta]


@dataclass(frozen=True, slots=True)
class DiffReport:
    """Two windows + per-bucket deltas."""

    span_seconds: float
    project: str | None
    current: WindowSnapshot
    previous: WindowSnapshot
    deltas: DeltaReport


def compute_diff(
    db: Database,
    *,
    span: timedelta = _DEFAULT_SPAN,
    project: str | None = None,
) -> DiffReport:
    """Run the stats aggregations over [now-span, now) and [now-2*span, now-span)."""
    if span.total_seconds() <= 0:
        msg = f"diff span must be positive, got {span!r}"
        raise ValueError(msg)
    now = datetime.now(UTC)
    cur_lower = now - span
    prev_lower = now - 2 * span
    ro = open_ro(db.path)
    try:
        current = _window(ro, lower=cur_lower, upper=now, project=project)
        previous = _window(ro, lower=prev_lower, upper=cur_lower, project=project)
    finally:
        ro.close()
    deltas = _compute_deltas(current=current, previous=previous)
    return DiffReport(
        span_seconds=span.total_seconds(),
        project=project,
        current=current,
        previous=previous,
        deltas=deltas,
    )


# --------------------------------------------------------------------------- window


def _window(
    conn: sqlite3.Connection,
    *,
    lower: datetime,
    upper: datetime,
    project: str | None,
) -> WindowSnapshot:
    lower_iso = _format_iso(lower)
    upper_iso = _format_iso(upper)
    return WindowSnapshot(
        lower=lower_iso,
        upper=upper_iso,
        tool_calls_total=_tool_calls_total(conn, lower_iso, upper_iso, project),
        tool_calls_by_name=_tool_calls_by_name(conn, lower_iso, upper_iso, project),
        commands_total=_commands_total(conn, lower_iso, upper_iso, project),
        commands_top=_commands_top(conn, lower_iso, upper_iso, project),
        sessions_count=_sessions_count(conn, lower_iso, upper_iso, project),
        sessions_median_seconds=_sessions_median(conn, lower_iso, upper_iso, project),
        sessions_p95_seconds=_sessions_p95(conn, lower_iso, upper_iso, project),
        files_count=_files_count(conn, lower_iso, upper_iso, project),
        model_histogram=_model_histogram(conn, lower_iso, upper_iso, project),
    )


# --------------------------------------------------------------------------- tool_calls


def _tool_call_where(lower: str, upper: str, project: str | None) -> tuple[str, list[object], bool]:
    where = ["tc.started_at IS NOT NULL", "tc.started_at >= ?", "tc.started_at < ?"]
    params: list[object] = [lower, upper]
    if project is not None:
        where.append("s.project_path = ?")
        params.append(project)
    return " WHERE " + " AND ".join(where), params, project is not None


def _tool_call_from(*, needs_join: bool) -> str:
    base = "FROM tool_calls tc"
    if needs_join:
        return base + " JOIN sessions s ON s.id = tc.session_id"
    return base


def _tool_calls_total(conn: sqlite3.Connection, lower: str, upper: str, project: str | None) -> int:
    where_sql, params, needs_join = _tool_call_where(lower, upper, project)
    sql = f"SELECT COUNT(*) {_tool_call_from(needs_join=needs_join)}{where_sql}"
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def _tool_calls_by_name(
    conn: sqlite3.Connection, lower: str, upper: str, project: str | None
) -> dict[str, int]:
    where_sql, params, needs_join = _tool_call_where(lower, upper, project)
    sql = (
        f"SELECT tc.name AS name, COUNT(*) AS n "
        f"{_tool_call_from(needs_join=needs_join)}"
        f"{where_sql} GROUP BY tc.name ORDER BY n DESC, tc.name LIMIT ?"
    )
    rows = conn.execute(sql, [*params, _TOOLS_TOP_LIMIT]).fetchall()
    return {str(r["name"]): int(r["n"]) for r in rows}


# --------------------------------------------------------------------------- commands


def _commands_query(
    conn: sqlite3.Connection, lower: str, upper: str, project: str | None
) -> list[str]:
    where = [
        "m.role = 'user'",
        "m.timestamp IS NOT NULL",
        "m.timestamp >= ?",
        "m.timestamp < ?",
    ]
    params: list[object] = [lower, upper]
    if project is not None:
        where.append("s.project_path = ?")
        params.append(project)
    where_sql = " AND ".join(where)
    base_sql = (
        "SELECT m.content AS content "
        "FROM messages m "
        "JOIN sessions s ON s.id = m.session_id "
        "JOIN ("
        "    SELECT session_id, MIN(seq) AS min_seq "
        "    FROM messages "
        "    WHERE role = 'user' "
        "    GROUP BY session_id"
        ") first ON first.session_id = m.session_id AND first.min_seq = m.seq "
        "WHERE "
    )
    sql = base_sql + where_sql  # WHERE built from fixed allow-list; binds parameterized.
    rows = conn.execute(sql, params).fetchall()
    return [_normalize_command(r["content"] if r["content"] is not None else "") for r in rows]


def _normalize_command(content: str) -> str:
    collapsed = " ".join(content.split())
    if len(collapsed) > _TRUNC_LEN:
        return collapsed[:_TRUNC_LEN]
    return collapsed


def _commands_total(conn: sqlite3.Connection, lower: str, upper: str, project: str | None) -> int:
    return len(_commands_query(conn, lower, upper, project))


def _commands_top(
    conn: sqlite3.Connection, lower: str, upper: str, project: str | None
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for cmd in _commands_query(conn, lower, upper, project):
        counts[cmd] = counts.get(cmd, 0) + 1
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(items[:_COMMANDS_TOP_LIMIT])


# --------------------------------------------------------------------------- sessions


def _sessions_durations(
    conn: sqlite3.Connection, lower: str, upper: str, project: str | None
) -> list[float]:
    where = ["started_at IS NOT NULL", "started_at >= ?", "started_at < ?"]
    params: list[object] = [lower, upper]
    if project is not None:
        where.append("project_path = ?")
        params.append(project)
    where_sql = " AND ".join(where)
    base_sql = (
        "SELECT (julianday(ended_at) - julianday(started_at)) * ? "
        "AS duration_s FROM sessions WHERE "
    )
    sql = base_sql + where_sql  # WHERE built from fixed allow-list; binds parameterized.
    bind_params: list[object] = [SECONDS_PER_DAY, *params]
    rows = conn.execute(sql, bind_params).fetchall()
    return [float(r["duration_s"]) for r in rows if r["duration_s"] is not None]


def _sessions_count(conn: sqlite3.Connection, lower: str, upper: str, project: str | None) -> int:
    where = ["started_at IS NOT NULL", "started_at >= ?", "started_at < ?"]
    params: list[object] = [lower, upper]
    if project is not None:
        where.append("project_path = ?")
        params.append(project)
    base_sql = "SELECT COUNT(*) FROM sessions WHERE "
    sql = base_sql + " AND ".join(where)  # WHERE from fixed allow-list; binds parameterized.
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def _sessions_median(
    conn: sqlite3.Connection, lower: str, upper: str, project: str | None
) -> float | None:
    durations = _sessions_durations(conn, lower, upper, project)
    if not durations:
        return None
    return float(statistics.median(durations))


def _sessions_p95(
    conn: sqlite3.Connection, lower: str, upper: str, project: str | None
) -> float | None:
    durations = _sessions_durations(conn, lower, upper, project)
    if len(durations) < _MIN_QUANTILE_SAMPLES:
        return None
    cuts = statistics.quantiles(durations, n=_QUANTILE_BUCKETS, method="exclusive")
    return float(cuts[_P95_QUANTILE_INDEX])


# --------------------------------------------------------------------------- files


def _files_count(conn: sqlite3.Connection, lower: str, upper: str, project: str | None) -> int:
    """Count distinct source_files whose owning sessions started in the window."""
    where = ["s.started_at IS NOT NULL", "s.started_at >= ?", "s.started_at < ?"]
    params: list[object] = [lower, upper]
    if project is not None:
        where.append("s.project_path = ?")
        params.append(project)
    where_sql = " AND ".join(where)
    base_sql = (
        "SELECT COUNT(DISTINCT sf.id) FROM source_files sf "
        "JOIN sessions s ON s.source_file_id = sf.id WHERE "
    )
    sql = base_sql + where_sql  # WHERE from fixed allow-list; binds parameterized.
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


# --------------------------------------------------------------------------- model


def _model_histogram(
    conn: sqlite3.Connection, lower: str, upper: str, project: str | None
) -> dict[str, int]:
    where = ["started_at IS NOT NULL", "started_at >= ?", "started_at < ?"]
    params: list[object] = [lower, upper]
    if project is not None:
        where.append("project_path = ?")
        params.append(project)
    where_sql = " AND ".join(where)
    base_sql = "SELECT model, COUNT(*) AS n FROM sessions WHERE "
    suffix = " GROUP BY model ORDER BY n DESC, model"
    sql = base_sql + where_sql + suffix  # WHERE from fixed allow-list; binds parameterized.
    rows = conn.execute(sql, params).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        model = r["model"]
        key = "(unknown)" if model is None or str(model).strip() == "" else str(model)
        out[key] = out.get(key, 0) + int(r["n"])
    return out


# --------------------------------------------------------------------------- deltas


def _delta_scalar(current: float, previous: float) -> Delta:
    absolute = current - previous
    pct = _pct_change(current, previous)
    return Delta(absolute=absolute, pct=pct)


def _pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return 0.0 if current == 0 else None
    return (current - previous) / previous


def _delta_optional(current: float | None, previous: float | None) -> Delta:
    cur = 0.0 if current is None else current
    prev = 0.0 if previous is None else previous
    return _delta_scalar(cur, prev)


def _delta_mapping(current: Mapping[str, int], previous: Mapping[str, int]) -> dict[str, Delta]:
    keys = set(current) | set(previous)
    return {
        k: _delta_scalar(float(current.get(k, 0)), float(previous.get(k, 0)))
        # Stable order: put higher-current first, then alphabetical.
        for k in sorted(keys, key=lambda kk: (-current.get(kk, 0), kk))
    }


def _compute_deltas(*, current: WindowSnapshot, previous: WindowSnapshot) -> DeltaReport:
    return DeltaReport(
        tool_calls_total=_delta_scalar(
            float(current.tool_calls_total), float(previous.tool_calls_total)
        ),
        tool_calls_by_name=_delta_mapping(current.tool_calls_by_name, previous.tool_calls_by_name),
        commands_total=_delta_scalar(float(current.commands_total), float(previous.commands_total)),
        commands_top=_delta_mapping(current.commands_top, previous.commands_top),
        sessions_count=_delta_scalar(float(current.sessions_count), float(previous.sessions_count)),
        sessions_median_seconds=_delta_optional(
            current.sessions_median_seconds, previous.sessions_median_seconds
        ),
        sessions_p95_seconds=_delta_optional(
            current.sessions_p95_seconds, previous.sessions_p95_seconds
        ),
        files_count=_delta_scalar(float(current.files_count), float(previous.files_count)),
        model_histogram=_delta_mapping(current.model_histogram, previous.model_histogram),
    )
