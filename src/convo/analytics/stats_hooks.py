"""`convo stats hooks` — guard decision frequency by hook and decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from convo.read._db_access import open_ro
from convo.read.filters import since_iso

if TYPE_CHECKING:
    from datetime import timedelta

    from convo.db import Database


_TOP_HOOK_LIMIT: int = 20


@dataclass(frozen=True, slots=True)
class HookFreq:
    """One row of hook decision counts."""

    hook_id: str
    count: int


@dataclass(frozen=True, slots=True)
class DecisionFreq:
    """Count of one decision verb across all hooks."""

    decision: str
    count: int


@dataclass(frozen=True, slots=True)
class HooksReport:
    """Aggregate guard decision statistics over a (since, project) window."""

    total: int
    top_by_hook: tuple[HookFreq, ...]
    by_decision: tuple[DecisionFreq, ...]


def stats_hooks(
    db: Database,
    *,
    since: timedelta | None = None,
    project: str | None = None,
) -> HooksReport:
    """Compute guard decision aggregates over the indexed log."""
    db_path = db.path
    where_parts: list[str] = []
    params: list[object] = []
    if since is not None:
        where_parts.append("timestamp >= ?")
        params.append(since_iso(since))
    if project is not None:
        where_parts.append("cwd = ?")
        params.append(project)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    # WHERE clause is composed from a fixed allow-list of fragments
    # (`timestamp >= ?`, `cwd = ?`); all user-supplied values are bound via
    # `?` placeholders. Same pattern used by other stats_*.py modules.
    with open_ro(db_path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM guard_decisions {where}",  # noqa: S608
            params,
        ).fetchone()[0]

        hooks = conn.execute(
            f"SELECT hook_id, COUNT(*) AS count FROM guard_decisions {where} "  # noqa: S608
            f"GROUP BY hook_id ORDER BY count DESC, hook_id LIMIT ?",
            (*params, _TOP_HOOK_LIMIT),
        ).fetchall()
        decisions = conn.execute(
            f"SELECT decision, COUNT(*) AS count FROM guard_decisions {where} "  # noqa: S608
            f"GROUP BY decision ORDER BY count DESC, decision",
            params,
        ).fetchall()

    return HooksReport(
        total=int(total),
        top_by_hook=tuple(HookFreq(hook_id=r["hook_id"], count=int(r["count"])) for r in hooks),
        by_decision=tuple(
            DecisionFreq(decision=r["decision"], count=int(r["count"])) for r in decisions
        ),
    )
