"""Integration tests for the v2 search agent-experience affordances."""

from __future__ import annotations

import json
import subprocess

import pytest


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


def test_default_limit_is_10(seeded_db_path: str) -> None:
    """Without --limit, returns at most 10 hits."""
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "kafka",
            "--format=json",
        ],
        text=True,
    )
    data = json.loads(out)
    hits = data.get("search", {}).get("hits") if "search" in data else data.get("hits", [])
    assert len(hits) <= 10
    filters_block = (
        data.get("search", {}).get("filters") if "search" in data else data.get("filters", {})
    )
    assert filters_block.get("limit") == 10


# ---------------------------------------------------------------------------
# Regression: _span_to_str/_WIDEN_TABLE mismatch — --since widen suggestions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "since_value",
    ["1d", "7d", "24h", "1y"],  # all map to keys in _WIDEN_TABLE
)
def test_zero_hits_widens_since(seeded_db_path: str, since_value: str) -> None:
    """For --since values that resolve to a _WIDEN_TABLE key, zero-hit search
    must emit a widen suggestion.  Regression guard for the _span_to_str /
    _WIDEN_TABLE mismatch where 7d was serialised as '1w' (no table entry)."""
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
            since_value,
        ],
        text=True,
    )
    assert "0 hits" in out
    assert "widen" in out.lower(), (
        f"--since {since_value!r} should emit a widen suggestion but didn't.\nGot:\n{out}"
    )


# ---------------------------------------------------------------------------
# Gap 1: --tool prefix matching end-to-end
# ---------------------------------------------------------------------------


def test_tool_prefix_filter_matches_partial_name(seeded_db_path: str) -> None:
    """`--tool B` should match all tools whose name starts with B (e.g. Bash)."""
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "kafka",
            "--tool",
            "B",
            "--format=json",
        ],
        text=True,
    )
    data = json.loads(out)
    hits = data["search"]["hits"]
    # At least one tool hit should be returned (Bash matches "B" prefix)
    tool_hits = [h for h in hits if h["kind"] in {"tool_call", "tool_result"}]
    assert tool_hits, "expected at least one tool hit with prefix 'B'"
    # All tool_call/tool_result hits should have tool names starting with "B"
    for hit in tool_hits:
        tool_name = hit.get("tool") or hit.get("tool_origin")
        assert tool_name is not None
        assert tool_name.startswith("B"), f"tool {tool_name!r} doesn't start with B"


# ---------------------------------------------------------------------------
# Gap 2: --tool-exact end-to-end
# ---------------------------------------------------------------------------


def test_tool_exact_filter_requires_exact_name(seeded_db_path: str) -> None:
    """`--tool B --tool-exact` should match NOTHING (no tool literally named 'B')."""
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "kafka",
            "--tool",
            "B",
            "--tool-exact",
            "--format=json",
        ],
        text=True,
    )
    data = json.loads(out)
    # No tool named exactly "B"; expected zero tool hits
    tool_hits = [h for h in data["search"]["hits"] if h["kind"] in {"tool_call", "tool_result"}]
    assert tool_hits == []


def test_tool_exact_with_full_name_matches(seeded_db_path: str) -> None:
    """`--tool Bash --tool-exact` should match Bash hits."""
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "kafka",
            "--tool",
            "Bash",
            "--tool-exact",
            "--format=json",
        ],
        text=True,
    )
    data = json.loads(out)
    hits = data["search"]["hits"]
    tool_hits = [h for h in hits if h["kind"] in {"tool_call", "tool_result"}]
    assert tool_hits, "expected at least one Bash hit"
    for hit in tool_hits:
        tool_name = hit.get("tool") or hit.get("tool_origin")
        assert tool_name == "Bash"


# ---------------------------------------------------------------------------
# Gap 3: --session prefix matching end-to-end
# ---------------------------------------------------------------------------


def test_session_prefix_filter(seeded_db_path: str, seeded_session_id: str) -> None:
    """`--session <prefix>` should restrict hits to that session."""
    short = seeded_session_id[:8]
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "kafka",
            "--session",
            short,
            "--format=json",
        ],
        text=True,
    )
    data = json.loads(out)
    hits = data["search"]["hits"]
    assert hits, "expected at least one hit for seeded session"
    for hit in hits:
        assert hit["session_id"].startswith(short)


# ---------------------------------------------------------------------------
# Gap 4: Multi-filter combination
# ---------------------------------------------------------------------------


def test_combined_since_project_tool_filters(seeded_db_path: str) -> None:
    """Filters compose correctly when all 3 are set."""
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "kafka",
            "--since",
            "30d",
            "--project",
            "tracine-ops",
            "--tool",
            "B",
            "--format=json",
        ],
        text=True,
    )
    data = json.loads(out)
    # No crash, valid envelope
    assert data["schema_version"] == 2
    for hit in data["search"]["hits"]:
        # Each filter must hold
        if hit["kind"] in {"tool_call", "tool_result"}:
            tool_name = hit.get("tool") or hit.get("tool_origin")
            assert tool_name is not None
            assert tool_name.startswith("B")
        assert "tracine-ops" in hit["project"]


# ---------------------------------------------------------------------------
# Gap 5: --fields exercising all advertised field names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["session", "ts", "when", "project", "kind", "excerpt", "command", "content", "output", "tool"],
)
def test_fields_projection_each_advertised_field(seeded_db_path: str, field: str) -> None:
    """Each advertised field name in --fields should produce valid TSV output without crashing."""
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
            field,
        ],
        text=True,
    )
    # Single-column projection: every non-empty line should have one value (no tabs)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, f"--fields {field!r} produced no output"
    for line in lines:
        # Tab count = 0 for single-field projection
        assert "\t" not in line or field in {"command", "content", "output", "excerpt"}


# ---------------------------------------------------------------------------
# Gap 6: drop_tool suggestion fires when --tool is set on empty result
# ---------------------------------------------------------------------------


def test_drop_tool_suggestion_on_zero_hits(seeded_db_path: str) -> None:
    """0-hit search with --tool should emit a 'drop --tool' suggestion."""
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "absolutelynothingmatcheszzz",
            "--tool",
            "Bash",
        ],
        text=True,
    )
    assert "0 hits" in out
    assert "Suggestions:" in out
    # The drop_tool description should mention Bash
    assert "Bash" in out


# ---------------------------------------------------------------------------
# Gap 7: drop_session suggestion fires when --session is set on empty result
# ---------------------------------------------------------------------------


def test_drop_session_suggestion_on_zero_hits(seeded_db_path: str, seeded_session_id: str) -> None:
    """0-hit search with --session should emit a 'drop --session' suggestion."""
    short = seeded_session_id[:8]
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "absolutelynothingmatcheszzz",
            "--session",
            short,
        ],
        text=True,
    )
    assert "0 hits" in out
    assert "Suggestions:" in out
    assert short in out


# ---------------------------------------------------------------------------
# Gap 8: --excerpt-chars 0 boundary
# ---------------------------------------------------------------------------


def test_excerpt_chars_zero_does_not_crash(seeded_db_path: str) -> None:
    """`--excerpt-chars 0` should produce valid output (snippet_tokens clamped to >= 1)."""
    out = subprocess.check_output(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            "convo",
            "--db",
            seeded_db_path,
            "search",
            "kafka",
            "--excerpt-chars",
            "0",
            "--limit",
            "3",
            "--format=json",
        ],
        text=True,
    )
    data = json.loads(out)
    # No crash, valid envelope
    assert data["schema_version"] == 2
    assert isinstance(data["search"]["hits"], list)
