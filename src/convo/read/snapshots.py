"""`convo snapshots` — list snapshot files in the snapshot directory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

SNAPSHOT_GLOB = "convo-*.db"
_FILENAME_FMT = "convo-%Y%m%d-%H%M%S-%f"

_SECONDS_PER_MINUTE: int = 60
_MINUTES_PER_HOUR: int = 60
_HOURS_PER_DAY: int = 24
_DAYS_PER_WEEK: int = 7


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    """One snapshot file entry."""

    path: Path
    timestamp_utc: datetime
    size_bytes: int
    age_human: str


def _parse_timestamp(stem: str) -> datetime | None:
    """Parse `convo-YYYYMMDD-HHMMSS-ffffff` stem to a UTC datetime, or None."""
    try:
        parsed = datetime.strptime(stem, _FILENAME_FMT)  # noqa: DTZ007 — replace below
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _format_age(td: timedelta) -> str:
    """Format a timedelta as the largest non-zero unit (e.g. `3h ago`, `2d ago`).

    Uses thresholds: <60s -> Ns, <60m -> Nm, <24h -> Nh, <7d -> Nd, else Nw.
    Negative deltas (clock skew) are clamped to 0s.
    """
    total = int(td.total_seconds())
    total = max(total, 0)
    if total < _SECONDS_PER_MINUTE:
        return f"{total}s ago"
    minutes = total // _SECONDS_PER_MINUTE
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes}m ago"
    hours = minutes // _MINUTES_PER_HOUR
    if hours < _HOURS_PER_DAY:
        return f"{hours}h ago"
    days = hours // _HOURS_PER_DAY
    if days < _DAYS_PER_WEEK:
        return f"{days}d ago"
    weeks = days // _DAYS_PER_WEEK
    return f"{weeks}w ago"


def list_snapshots(snapshot_dir: Path) -> list[SnapshotInfo]:
    """List `convo-*.db` snapshots in `snapshot_dir`, sorted newest-first.

    Files whose names don't match the `convo-YYYYMMDD-HHMMSS-ffffff.db` pattern
    are skipped (defensive — the glob shouldn't match such files).
    Missing or non-directory `snapshot_dir` returns an empty list.
    """
    if not snapshot_dir.exists() or not snapshot_dir.is_dir():
        return []

    now = datetime.now(UTC)
    out: list[SnapshotInfo] = []
    for entry in snapshot_dir.glob(SNAPSHOT_GLOB):
        if not entry.is_file():
            continue
        ts = _parse_timestamp(entry.stem)
        if ts is None:
            continue
        out.append(
            SnapshotInfo(
                path=entry,
                timestamp_utc=ts,
                size_bytes=entry.stat().st_size,
                age_human=_format_age(now - ts),
            ),
        )
    out.sort(key=lambda s: s.timestamp_utc, reverse=True)
    return out
