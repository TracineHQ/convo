"""`convo projects` — list indexed projects with hit counts and recency."""

from __future__ import annotations

from dataclasses import dataclass

from convo.read._db_access import open_ro
from convo.read.filters import top_level_sessions


@dataclass(frozen=True, slots=True)
class ProjectRow:
    path: str
    sessions: int
    last_seen: str | None


def list_projects(db_path: str, *, include_subagents: bool = False) -> list[ProjectRow]:
    """List distinct projects with session counts and last_seen timestamps.

    Subagent sessions are left out unless `include_subagents`.
    """
    base_sql = (
        "SELECT project_path AS path, COUNT(*) AS sessions, "
        "       MAX(COALESCE(ended_at, started_at)) AS last_seen "
        "FROM sessions "
        "WHERE project_path IS NOT NULL"
    )
    scope = "" if include_subagents else " AND " + top_level_sessions()
    tail = " GROUP BY project_path ORDER BY last_seen DESC NULLS LAST"
    ro = open_ro(db_path)
    try:
        rows = ro.execute(base_sql + scope + tail).fetchall()
    finally:
        ro.close()
    return [
        ProjectRow(
            path=str(r["path"]),
            sessions=int(r["sessions"]),
            last_seen=None if r["last_seen"] is None else str(r["last_seen"]),
        )
        for r in rows
    ]
