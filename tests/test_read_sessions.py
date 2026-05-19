"""Integration tests for `convo sessions`."""

from __future__ import annotations

import json
import subprocess


def test_sessions_lists_items(seeded_db_path: str) -> None:
    cmd = ["uv", "run", "convo", "--db", seeded_db_path, "sessions", "--format=json"]
    out = subprocess.check_output(cmd, text=True)  # noqa: S603
    data = json.loads(out)
    items = data.get("sessions", {}).get("items") if "sessions" in data else data.get("items", [])
    assert len(items) >= 2  # seeded fixture has 3 sessions
    for s in items:
        assert "id" in s
        assert "project_path" in s
        assert "started_at" in s
        assert "ended_at" in s
        assert "message_count" in s


def test_sessions_prose_default(seeded_db_path: str) -> None:
    cmd = ["uv", "run", "convo", "--db", seeded_db_path, "sessions"]
    out = subprocess.check_output(cmd, text=True)  # noqa: S603
    # Should contain at least one session id from the seed.
    assert "sess-1" in out or "sess-2" in out or "sess-3" in out


def test_sessions_sorted_by_recency(seeded_db_path: str) -> None:
    cmd = ["uv", "run", "convo", "--db", seeded_db_path, "sessions", "--format=json"]
    out = subprocess.check_output(cmd, text=True)  # noqa: S603
    data = json.loads(out)
    items = data.get("sessions", {}).get("items") if "sessions" in data else data.get("items", [])
    started_ats = [item["started_at"] for item in items if item["started_at"]]
    assert started_ats == sorted(started_ats, reverse=True)


def test_sessions_project_filter(seeded_db_path: str) -> None:
    cmd = [
        "uv",
        "run",
        "convo",
        "--db",
        seeded_db_path,
        "sessions",
        "--project",
        "tracine-ops",
        "--format=json",
    ]
    out = subprocess.check_output(cmd, text=True)  # noqa: S603
    data = json.loads(out)
    items = data.get("sessions", {}).get("items") if "sessions" in data else data.get("items", [])
    assert len(items) >= 1
    for s in items:
        assert s["project_path"] is not None
        assert "tracine-ops" in s["project_path"]
