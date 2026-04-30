"""Tests for `list_snapshots` in `convo.read.snapshots`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from convo.read.snapshots import SnapshotInfo, _format_age, list_snapshots

if TYPE_CHECKING:
    from pathlib import Path


def _write_snapshot(
    snapshot_dir: Path,
    *,
    when: datetime,
    payload: bytes = b"x",
) -> Path:
    """Write a fake snapshot file whose name encodes `when` (UTC microseconds)."""
    name = when.strftime("convo-%Y%m%d-%H%M%S-%f.db")
    path = snapshot_dir / name
    path.write_bytes(payload)
    return path


def test_list_snapshots_sorted_newest_first(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    now = datetime.now(UTC)
    older = _write_snapshot(snapshot_dir, when=now - timedelta(days=3), payload=b"a" * 10)
    middle = _write_snapshot(snapshot_dir, when=now - timedelta(hours=5), payload=b"b" * 20)
    newest = _write_snapshot(snapshot_dir, when=now - timedelta(minutes=10), payload=b"c" * 30)

    snapshots = list_snapshots(snapshot_dir)

    assert [s.path for s in snapshots] == [newest, middle, older]
    assert all(isinstance(s, SnapshotInfo) for s in snapshots)
    assert snapshots[0].size_bytes == 30
    assert snapshots[1].size_bytes == 20
    assert snapshots[2].size_bytes == 10
    assert "ago" in snapshots[0].age_human


def test_list_snapshots_missing_dir(tmp_path: Path) -> None:
    assert list_snapshots(tmp_path / "nope") == []


def test_list_snapshots_skips_non_matching_files(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    when = datetime.now(UTC) - timedelta(hours=1)
    valid = _write_snapshot(snapshot_dir, when=when)
    # Glob `convo-*.db` matches this, but the stem won't parse as a timestamp.
    bad = snapshot_dir / "convo-not-a-timestamp.db"
    bad.write_bytes(b"junk")
    # Random files in dir get filtered by glob anyway.
    (snapshot_dir / "README.txt").write_text("hi")

    snapshots = list_snapshots(snapshot_dir)
    assert [s.path for s in snapshots] == [valid]


def test_list_snapshots_skips_subdirectories(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    # A directory whose name matches the glob — must not be returned.
    fake = snapshot_dir / "convo-20260101-000000-000000.db"
    fake.mkdir()
    assert list_snapshots(snapshot_dir) == []


def test_list_snapshots_path_is_file_not_dir(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    assert list_snapshots(not_a_dir) == []


def test_format_age_seconds() -> None:
    assert _format_age(timedelta(seconds=0)) == "0s ago"
    assert _format_age(timedelta(seconds=30)) == "30s ago"
    assert _format_age(timedelta(seconds=59)) == "59s ago"


def test_format_age_minutes() -> None:
    assert _format_age(timedelta(minutes=1)) == "1m ago"
    assert _format_age(timedelta(minutes=45)) == "45m ago"
    assert _format_age(timedelta(minutes=59, seconds=59)) == "59m ago"


def test_format_age_hours() -> None:
    assert _format_age(timedelta(hours=1)) == "1h ago"
    assert _format_age(timedelta(hours=3)) == "3h ago"
    assert _format_age(timedelta(hours=23, minutes=59)) == "23h ago"


def test_format_age_days() -> None:
    assert _format_age(timedelta(days=1)) == "1d ago"
    assert _format_age(timedelta(days=2)) == "2d ago"
    assert _format_age(timedelta(days=6, hours=23)) == "6d ago"


def test_format_age_weeks() -> None:
    assert _format_age(timedelta(days=7)) == "1w ago"
    assert _format_age(timedelta(days=14)) == "2w ago"
    assert _format_age(timedelta(days=365)) == "52w ago"


def test_format_age_negative_clock_skew_clamps_to_zero() -> None:
    assert _format_age(timedelta(seconds=-5)) == "0s ago"


def test_list_snapshots_timestamp_utc_parsed_from_filename(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    when = datetime(2026, 4, 30, 12, 34, 56, 789012, tzinfo=UTC)
    _write_snapshot(snapshot_dir, when=when)

    snapshots = list_snapshots(snapshot_dir)
    assert len(snapshots) == 1
    assert snapshots[0].timestamp_utc == when


def test_list_snapshots_real_size_from_disk(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    when = datetime.now(UTC) - timedelta(minutes=2)
    path = _write_snapshot(snapshot_dir, when=when, payload=b"hello world")

    snapshots = list_snapshots(snapshot_dir)
    assert snapshots[0].size_bytes == path.stat().st_size
