"""`convo stats model` — sessions-per-model histogram + null/unknown count."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from convo.read._db_access import open_ro
from convo.read.filters import since_iso

if TYPE_CHECKING:
    from datetime import timedelta

    from convo.db import Database


@dataclass(frozen=True, slots=True)
class ModelCount:
    """One row of the model histogram."""

    model: str
    session_count: int


@dataclass(frozen=True, slots=True)
class ModelReport:
    """Aggregate session-by-model stats."""

    total_sessions: int
    null_count: int
    by_model: tuple[ModelCount, ...]


def stats_model(
    db: Database,
    *,
    since: timedelta | None = None,
    project: str | None = None,
) -> ModelReport:
    """Histogram of sessions per `sessions.model`. Null/empty rolled up separately."""
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

    base_sql = "SELECT model, COUNT(*) AS n FROM sessions"
    sql = base_sql + where_sql + " GROUP BY model ORDER BY n DESC, model"

    ro = open_ro(db.path)
    try:
        rows = ro.execute(sql, params).fetchall()
    finally:
        ro.close()

    null_count = 0
    by_model: list[ModelCount] = []
    total = 0
    for r in rows:
        n = int(r["n"])
        total += n
        model = r["model"]
        if model is None or str(model).strip() == "":
            null_count += n
            continue
        by_model.append(ModelCount(model=str(model), session_count=n))
    return ModelReport(
        total_sessions=total,
        null_count=null_count,
        by_model=tuple(by_model),
    )
