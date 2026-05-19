"""Integration tests for `convo projects`."""

from __future__ import annotations

import json
import subprocess


def test_projects_lists_distinct(seeded_db_path: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        ["uv", "run", "convo", "--db", seeded_db_path, "projects", "--format=json"],  # noqa: S607
        text=True,
    )
    data = json.loads(out)
    items = data.get("projects", {}).get("items") if "projects" in data else data.get("items", [])
    assert len(items) >= 2  # seeded fixture has 4 projects
    for p in items:
        assert "path" in p
        assert "sessions" in p
        assert "last_seen" in p


def test_projects_prose_default(seeded_db_path: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        ["uv", "run", "convo", "--db", seeded_db_path, "projects"],  # noqa: S607
        text=True,
    )
    # Should contain at least one project from the seed (4 projects exist).
    assert "tracine-ops" in out or "convo" in out or "ai-toolkit" in out


def test_projects_sorted_by_recency(seeded_db_path: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        ["uv", "run", "convo", "--db", seeded_db_path, "projects", "--format=json"],  # noqa: S607
        text=True,
    )
    data = json.loads(out)
    items = data.get("projects", {}).get("items") if "projects" in data else data.get("items", [])
    last_seens = [item["last_seen"] for item in items if item["last_seen"]]
    assert last_seens == sorted(last_seens, reverse=True)
