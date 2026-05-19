"""All commands emit `{schema_version: 2, <command>: {...}}` shape."""

from __future__ import annotations

import json
import subprocess

import pytest


@pytest.mark.parametrize(
    ("argv", "command_key"),
    [
        (["info", "--json"], "info"),
        (["search", "kafka", "--format=json"], "search"),
        (["inspect", "<sid>", "--json"], "inspect"),
        (["summary", "--since", "31d", "--json"], "summary"),
        (["diff", "--since", "31d", "--json"], "diff"),
        (["stats", "tools", "--json"], "stats"),
        (["projects", "--format=json"], "projects"),
        (["tools", "--format=json"], "tools"),
        (["sessions", "--format=json"], "sessions"),
        (["snapshots", "--json"], "snapshots"),
    ],
)
def test_envelope_shape(
    seeded_db_path: str,
    seeded_session_id: str,
    argv: list[str],
    command_key: str,
) -> None:
    """Verify schema_version=2 and the command-named wrapper key."""
    argv_filled = [s.replace("<sid>", seeded_session_id) for s in argv]
    out = subprocess.check_output(  # noqa: S603
        ["uv", "run", "convo", "--db", seeded_db_path, *argv_filled],  # noqa: S607
        text=True,
    )
    data = json.loads(out)
    assert data["schema_version"] == 2
    assert command_key in data
