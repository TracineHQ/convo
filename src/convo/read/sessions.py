"""`convo sessions` — list sessions with optional project/time filters."""

from __future__ import annotations

from dataclasses import dataclass

from convo.read._db_access import open_ro


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: str
    project_path: str | None
    started_at: str | None
    ended_at: str | None
    message_count: int


def list_sessions(
    db_path: str,
    *,
    project: str | None = None,
    since_iso: str | None = None,
    limit: int = 50,
) -> list[SessionRow]:
    """List sessions, optionally filtered by project + time, with msg counts."""
    conditions: list[str] = []
    params: list[object] = []

    if project is not None:
        conditions.append("s.project_path = ?")
        params.append(project)

    if since_iso is not None:
        conditions.append("s.started_at >= ?")
        params.append(since_iso)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    params.append(limit)

    ro = open_ro(db_path)
    try:
        rows = ro.execute(
            f"SELECT s.id, s.project_path, s.started_at, s.ended_at, "  # noqa: S608
            f"       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS msg_count "
            f"FROM sessions s "
            f"{where} "
            f"ORDER BY s.started_at DESC LIMIT ?",
            params,
        ).fetchall()
    finally:
        ro.close()

    return [
        SessionRow(
            id=str(r["id"]),
            project_path=None if r["project_path"] is None else str(r["project_path"]),
            started_at=None if r["started_at"] is None else str(r["started_at"]),
            ended_at=None if r["ended_at"] is None else str(r["ended_at"]),
            message_count=int(r["msg_count"]),
        )
        for r in rows
    ]
