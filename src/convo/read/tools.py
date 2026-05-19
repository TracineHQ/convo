"""`convo tools` — list distinct tool names with call counts and recency."""

from __future__ import annotations

from dataclasses import dataclass

from convo.read._db_access import open_ro


@dataclass(frozen=True, slots=True)
class ToolRow:
    name: str
    calls: int
    last_seen: str | None


def list_tools(db_path: str) -> list[ToolRow]:
    """List distinct tool names with call counts and last_seen timestamps."""
    ro = open_ro(db_path)
    try:
        rows = ro.execute(
            "SELECT name, COUNT(*) AS calls, MAX(started_at) AS last_seen "
            "FROM tool_calls "
            "GROUP BY name "
            "ORDER BY last_seen DESC NULLS LAST"
        ).fetchall()
    finally:
        ro.close()
    return [
        ToolRow(
            name=str(r["name"]),
            calls=int(r["calls"]),
            last_seen=None if r["last_seen"] is None else str(r["last_seen"]),
        )
        for r in rows
    ]
