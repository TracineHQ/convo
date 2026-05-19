"""Integration tests for `convo tools`."""

from __future__ import annotations

import json
import subprocess


def test_tools_lists_distinct(seeded_db_path: str) -> None:
    cmd = ["uv", "run", "convo", "--db", seeded_db_path, "tools", "--format=json"]
    out = subprocess.check_output(cmd, text=True)  # noqa: S603
    data = json.loads(out)
    items = data.get("tools", {}).get("items") if "tools" in data else data.get("items", [])
    assert len(items) >= 2  # seeded fixture has Bash, Read, Edit
    for t in items:
        assert "name" in t
        assert "calls" in t
        assert "last_seen" in t


def test_tools_prose_default(seeded_db_path: str) -> None:
    cmd = ["uv", "run", "convo", "--db", seeded_db_path, "tools"]
    out = subprocess.check_output(cmd, text=True)  # noqa: S603
    # Should contain at least one tool from the seed (Bash/Read/Edit).
    assert "Bash" in out or "Read" in out or "Edit" in out


def test_tools_sorted_by_recency(seeded_db_path: str) -> None:
    cmd = ["uv", "run", "convo", "--db", seeded_db_path, "tools", "--format=json"]
    out = subprocess.check_output(cmd, text=True)  # noqa: S603
    data = json.loads(out)
    items = data.get("tools", {}).get("items") if "tools" in data else data.get("items", [])
    last_seens = [item["last_seen"] for item in items if item["last_seen"]]
    assert last_seens == sorted(last_seens, reverse=True)
