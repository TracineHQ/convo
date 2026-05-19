"""`convo projects` — list indexed projects with hit counts and recency."""

from __future__ import annotations

from dataclasses import dataclass

from convo.read._db_access import open_ro


@dataclass(frozen=True, slots=True)
class ProjectRow:
    path: str
    sessions: int
    last_seen: str | None


def list_projects(db_path: str) -> list[ProjectRow]:
    """List distinct projects with session counts and last_seen timestamps."""
    ro = open_ro(db_path)
    try:
        rows = ro.execute(
            "SELECT project_path AS path, COUNT(*) AS sessions, "
            "       MAX(COALESCE(ended_at, started_at)) AS last_seen "
            "FROM sessions "
            "WHERE project_path IS NOT NULL "
            "GROUP BY project_path "
            "ORDER BY last_seen DESC NULLS LAST"
        ).fetchall()
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
