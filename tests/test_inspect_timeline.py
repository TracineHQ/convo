"""Integration tests for `convo inspect --timeline`."""

from __future__ import annotations

import json
import subprocess


def test_timeline_emits_per_message_lines(seeded_db_path: str, seeded_session_id: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "inspect",
            seeded_session_id,
            "--timeline",
        ],
        text=True,
    )
    assert f"session: {seeded_session_id}" in out
    assert "duration:" in out
    # Per-row: "HH:MM:SS  role     tool    preview"
    lines = out.splitlines()
    timeline_rows = [ln for ln in lines if len(ln) >= 8 and ln[2] == ":" and ln[5] == ":"]
    assert timeline_rows, f"no timeline rows found in:\n{out}"


def test_timeline_with_message_range(seeded_db_path: str, seeded_session_id: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "inspect",
            seeded_session_id,
            "--timeline",
            "--from-message",
            "1",
            "--to-message",
            "3",
        ],
        text=True,
    )
    assert "Showing messages from 1" in out or "1 of" in out


def test_timeline_header_includes_project(seeded_db_path: str, seeded_session_id: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "inspect",
            seeded_session_id,
            "--timeline",
        ],
        text=True,
    )
    assert "project:" in out


def test_inspect_default_caps_at_50(seeded_db_path: str, seeded_long_session_id: str) -> None:
    """Default inspect output is capped at 50 messages."""
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "inspect",
            seeded_long_session_id,
            "--json",
        ],
        text=True,
    )
    data = json.loads(out)
    msgs = (
        data.get("inspect", {}).get("messages") if "inspect" in data else data.get("messages", [])
    )
    assert len(msgs) <= 50
    truncated = (
        data.get("inspect", {}).get("truncated") if "inspect" in data else data.get("truncated")
    )
    assert truncated is True


def test_inspect_full_disables_cap(seeded_db_path: str, seeded_long_session_id: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "inspect",
            seeded_long_session_id,
            "--full",
            "--json",
        ],
        text=True,
    )
    data = json.loads(out)
    truncated = (
        data.get("inspect", {}).get("truncated") if "inspect" in data else data.get("truncated")
    )
    assert truncated is False
    msgs = (
        data.get("inspect", {}).get("messages") if "inspect" in data else data.get("messages", [])
    )
    # seeded_long_session_id has 62 messages
    assert len(msgs) >= 60
