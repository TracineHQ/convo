"""`convo stats commands` — frequency of "first user message" across sessions.

A "command" is the first user message of a session (lowest seq among
`messages WHERE role='user'` per session). Groups are formed after normalizing
whitespace and truncating to 80 chars so near-duplicates collapse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from convo.read._db_access import open_ro
from convo.read.filters import since_iso

if TYPE_CHECKING:
    from datetime import timedelta

    from convo.db import Database


_TOP_LIMIT: int = 20
_TRUNC_LEN: int = 80
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class CommandFreq:
    """One row of the commands histogram."""

    command: str
    count: int


@dataclass(frozen=True, slots=True)
class CommandsReport:
    """Aggregate "first user message" frequency over a (since, project) window."""

    total: int
    top_commands: tuple[CommandFreq, ...]


def stats_commands(
    db: Database,
    *,
    since: timedelta | None = None,
    project: str | None = None,
) -> CommandsReport:
    """Return the top-N first-user-message commands."""
    cutoff = since_iso(since)
    where: list[str] = ["m.role = 'user'"]
    params: list[object] = []
    if cutoff is not None:
        where.append("m.timestamp IS NOT NULL AND m.timestamp >= ?")
        params.append(cutoff)
    if project is not None:
        where.append("s.project_path = ?")
        params.append(project)

    # Subquery: for each session, find the MIN(seq) among that session's user
    # messages, then JOIN back to fetch the content.
    # WHERE clause is built from a fixed allow-list above; binds are parameterized.
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
    sql = base_sql + where_sql

    ro = open_ro(db.path)
    try:
        rows = ro.execute(sql, params).fetchall()
    finally:
        ro.close()

    counts: dict[str, int] = {}
    for r in rows:
        raw = r["content"]
        normalized = _normalize(raw if raw is not None else "")
        counts[normalized] = counts.get(normalized, 0) + 1

    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top = tuple(CommandFreq(command=k, count=v) for k, v in items[:_TOP_LIMIT])
    total = sum(counts.values())
    return CommandsReport(total=total, top_commands=top)


def _normalize(content: str) -> str:
    """Collapse whitespace, strip, truncate to 80 chars."""
    collapsed = _WS_RE.sub(" ", content).strip()
    if len(collapsed) > _TRUNC_LEN:
        return collapsed[:_TRUNC_LEN]
    return collapsed
