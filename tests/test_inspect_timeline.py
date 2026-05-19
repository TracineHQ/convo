"""Integration tests for `convo inspect --timeline`."""

from __future__ import annotations

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
