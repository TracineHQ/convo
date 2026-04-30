"""`convo info` — gather an overview report for the indexed DB."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from convo.db import resolve_snapshot_dir
from convo.read.snapshots import SNAPSHOT_GLOB

if TYPE_CHECKING:
    from pathlib import Path

    from convo.db import Database

_ERR_DB_NOT_OPEN = "Database is not open"
_COUNTED_TABLES: tuple[str, ...] = (
    "source_files",
    "sessions",
    "messages",
    "tool_calls",
    "tool_results",
)


@dataclass(frozen=True, slots=True)
class ProjectCount:
    """One row of the `top projects by session count` summary."""

    project_path: str | None
    session_count: int


@dataclass(frozen=True, slots=True)
class InfoReport:
    """Aggregate, read-only snapshot of the convo DB's current state."""

    schema_version: int
    row_counts: dict[str, int]
    last_indexed_at: str | None
    top_projects: list[ProjectCount]
    db_size_bytes: int
    snapshot_dir_path: Path
    snapshot_count: int
    snapshot_total_bytes: int


def _row_counts(db: Database) -> dict[str, int]:
    assert db.conn is not None
    counts: dict[str, int] = {}
    for table in _COUNTED_TABLES:
        # Table names are a fixed allow-list defined above; safe to interpolate.
        row = db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
        counts[table] = int(row[0])
    return counts


def _last_indexed_at(db: Database) -> str | None:
    assert db.conn is not None
    row = db.conn.execute("SELECT MAX(last_indexed_at) FROM source_files").fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _top_projects(db: Database, *, limit: int = 5) -> list[ProjectCount]:
    assert db.conn is not None
    rows = db.conn.execute(
        "SELECT project_path, COUNT(*) AS n "
        "FROM sessions "
        "GROUP BY project_path "
        "ORDER BY n DESC, project_path "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[ProjectCount] = []
    for row in rows:
        project = row["project_path"]
        path: str | None = None if project is None else str(project)
        out.append(ProjectCount(project_path=path, session_count=int(row["n"])))
    return out


def _snapshot_stats(snapshot_dir: Path) -> tuple[int, int]:
    if not snapshot_dir.exists() or not snapshot_dir.is_dir():
        return (0, 0)
    count = 0
    total = 0
    for entry in snapshot_dir.glob(SNAPSHOT_GLOB):
        if not entry.is_file():
            continue
        count += 1
        total += entry.stat().st_size
    return (count, total)


def gather_info(db: Database) -> InfoReport:
    """Collect an `InfoReport` from an opened `Database`.

    Read-only: issues only `SELECT`/`PRAGMA` and a filesystem stat. The DB must
    already be opened by the caller.
    """
    if db.conn is None:
        raise RuntimeError(_ERR_DB_NOT_OPEN)

    schema_version = int(db.conn.execute("PRAGMA user_version").fetchone()[0])
    counts = _row_counts(db)
    last_at = _last_indexed_at(db)
    projects = _top_projects(db)
    db_size = db.path.stat().st_size
    snapshot_dir = resolve_snapshot_dir(None, db.path)
    snapshot_count, snapshot_total = _snapshot_stats(snapshot_dir)

    return InfoReport(
        schema_version=schema_version,
        row_counts=counts,
        last_indexed_at=last_at,
        top_projects=projects,
        db_size_bytes=db_size,
        snapshot_dir_path=snapshot_dir,
        snapshot_count=snapshot_count,
        snapshot_total_bytes=snapshot_total,
    )
