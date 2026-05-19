"""Integration tests for the v2 search agent-experience affordances."""

from __future__ import annotations

import json
import subprocess


def test_search_prose_default(seeded_db_path: str) -> None:
    """Default invocation emits structured prose, not JSON."""
    out = subprocess.check_output(  # noqa: S603
        ["uv", "run", "convo", "--db", seeded_db_path, "search", "kafka"],  # noqa: S607
        text=True,
    )
    assert "--- hit " in out
    assert "session:" in out
    assert "when:" in out
    assert not out.lstrip().startswith("{")


def test_search_json_via_format(seeded_db_path: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        ["uv", "run", "convo", "--db", seeded_db_path, "search", "kafka", "--format=json"],  # noqa: S607
        text=True,
    )
    data = json.loads(out)
    assert isinstance(data, dict)
    # Either the v1 top-level "hits" or the v2 wrapped "search" key is acceptable
    # at this checkpoint; Task 17 unifies to {"schema_version": 2, "search": {...}}.
    assert "search" in data or "hits" in data


def test_search_json_legacy_flag(seeded_db_path: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        ["uv", "run", "convo", "--db", seeded_db_path, "search", "kafka", "--json"],  # noqa: S607
        text=True,
    )
    data = json.loads(out)
    assert isinstance(data, dict)


def test_zero_hits_includes_suggestions(seeded_db_path: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "absolutelynothingmatcheszzz",
            "--since",
            "1d",
        ],
        text=True,
    )
    assert "0 hits" in out
    # since=1d set → should suggest widen
    assert "widen" in out.lower() or "Suggestions" in out


def test_indices_in_json(seeded_db_path: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        ["uv", "run", "convo", "--db", seeded_db_path, "search", "kafka", "--format=json"],  # noqa: S607
        text=True,
    )
    data = json.loads(out)
    hits = data.get("search", {}).get("hits") if "search" in data else data.get("hits", [])
    for hit in hits:
        assert "indices" in hit
        assert isinstance(hit["indices"], list)


def test_fields_projection_session_kind(seeded_db_path: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "kafka",
            "--fields",
            "session,kind",
        ],
        text=True,
    )
    lines = [line_item for line_item in out.splitlines() if line_item.strip()]
    assert lines, "expected at least one projected hit line"
    for line in lines:
        parts = line.split("\t")
        assert len(parts) == 2
        assert parts[1] in {"message", "tool_call", "tool_result"}


def test_fields_projection_when_excerpt(seeded_db_path: str) -> None:
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "kafka",
            "--fields",
            "when,excerpt",
        ],
        text=True,
    )
    lines = [line_item for line_item in out.splitlines() if line_item.strip()]
    assert lines
    for line in lines:
        parts = line.split("\t")
        assert len(parts) == 2
        # First column is a timestamp; second is excerpt with [match] markers
        assert "[" in parts[1]
        assert "]" in parts[1]
