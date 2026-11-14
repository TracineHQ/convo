"""`convo stats files` — source_files counts, total size, top by message_count."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from convo.intake.subagents import SUBAGENT_SESSION_LIKE
from convo.read._db_access import open_ro
from convo.read.filters import since_iso, top_level_sessions

if TYPE_CHECKING:
    import sqlite3
    from datetime import timedelta

    from convo.db import Database


_TOP_LIMIT: int = 10
# A subagent's transcript is a source file whose session has a dotted id. The
# only interpolated value is the module constant `SUBAGENT_SESSION_LIKE`.
_NO_SUBAGENT_FILES = (
    " WHERE NOT EXISTS (SELECT 1 FROM sessions sub "  # noqa: S608
    "WHERE sub.source_file_id = source_files.id "
    f"AND sub.id LIKE '{SUBAGENT_SESSION_LIKE}')"
)


@dataclass(frozen=True, slots=True)
class FileActivity:
    """One row of the most-active-files table."""

    path: str
    message_count: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class FilesReport:
    """Aggregate source_files statistics."""

    total: int
    total_size_bytes: int
    total_message_count: int
    top_files: tuple[FileActivity, ...]


def stats_files(
    db: Database,
    *,
    since: timedelta | None = None,
    project: str | None = None,
    include_subagents: bool = False,
) -> FilesReport:
    """Aggregate source_files row counts, sizes, and top-N by message_count.

    `since` filters by `sessions.started_at` of the file's owning session(s).
    `project` filters by `sessions.project_path`. When either filter is set, we
    scope to source_files joined through sessions. Subagent transcripts are
    source files too, and are left out unless `include_subagents`.
    """
    cutoff = since_iso(since)
    ro = open_ro(db.path)
    try:
        if cutoff is None and project is None:
            return _unfiltered(ro, include_subagents=include_subagents)
        return _filtered(ro, cutoff=cutoff, project=project, include_subagents=include_subagents)
    finally:
        ro.close()


def _unfiltered(conn: sqlite3.Connection, *, include_subagents: bool) -> FilesReport:
    scope = "" if include_subagents else _NO_SUBAGENT_FILES
    base_aggregate = (
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(size), 0) AS sz, "
        "COALESCE(SUM(message_count), 0) AS mc "
        "FROM source_files"
    )
    row = conn.execute(base_aggregate + scope).fetchone()
    total_files = int(row["n"]) if row is not None else 0
    total_size = int(row["sz"]) if row is not None else 0
    total_msgs = int(row["mc"]) if row is not None else 0

    base_top = "SELECT path, message_count, size FROM source_files"
    top_sql = base_top + scope + " ORDER BY message_count DESC, path LIMIT ?"
    top_rows = conn.execute(top_sql, (_TOP_LIMIT,)).fetchall()
    top = tuple(
        FileActivity(
            path=str(r["path"]),
            message_count=int(r["message_count"]),
            size_bytes=int(r["size"]),
        )
        for r in top_rows
    )
    return FilesReport(
        total=total_files,
        total_size_bytes=total_size,
        total_message_count=total_msgs,
        top_files=top,
    )


def _filtered(
    conn: sqlite3.Connection,
    *,
    cutoff: str | None,
    project: str | None,
    include_subagents: bool,
) -> FilesReport:
    where: list[str] = []
    params: list[object] = []
    if not include_subagents:
        where.append(top_level_sessions("s.id"))
    if cutoff is not None:
        where.append("s.started_at IS NOT NULL AND s.started_at >= ?")
        params.append(cutoff)
    if project is not None:
        where.append("s.project_path = ?")
        params.append(project)
    where_sql = " WHERE " + " AND ".join(where)

    # DISTINCT source_files rows whose sessions match the filter.
    base_aggregate = (
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(size), 0) AS sz, "
        "COALESCE(SUM(message_count), 0) AS mc "
        "FROM ("
        "    SELECT DISTINCT sf.id AS id, sf.size AS size, sf.message_count AS message_count "
        "    FROM source_files sf "
        "    JOIN sessions s ON s.source_file_id = sf.id"
    )
    aggregate_sql = base_aggregate + where_sql + ")"

    row = conn.execute(aggregate_sql, params).fetchone()
    total_files = int(row["n"]) if row is not None else 0
    total_size = int(row["sz"]) if row is not None else 0
    total_msgs = int(row["mc"]) if row is not None else 0

    base_top = (
        "SELECT DISTINCT sf.path AS path, sf.message_count AS message_count, sf.size AS size "
        "FROM source_files sf "
        "JOIN sessions s ON s.source_file_id = sf.id"
    )
    top_sql = base_top + where_sql + " ORDER BY sf.message_count DESC, sf.path LIMIT ?"
    top_rows = conn.execute(top_sql, [*params, _TOP_LIMIT]).fetchall()
    top = tuple(
        FileActivity(
            path=str(r["path"]),
            message_count=int(r["message_count"]),
            size_bytes=int(r["size"]),
        )
        for r in top_rows
    )
    return FilesReport(
        total=total_files,
        total_size_bytes=total_size,
        total_message_count=total_msgs,
        top_files=top,
    )
