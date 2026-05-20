"""Tests for structured-prose renderer.

NOTE: these tests rely on SearchHit having ``role`` and ``tool_origin`` fields
(added in Task 8). Until Task 8 lands, these tests fail with TypeError.
"""

from __future__ import annotations

from convo.read.prose import SearchRenderConfig, render_search_hits
from convo.read.search import SearchHit


def _hit(**overrides: object) -> SearchHit:
    base: dict[str, object] = {
        "kind": "message",
        "id": "msg-001",
        "session_id": "7e0ac728-6883-4ad0-912f-1e1a7f4e62ff",
        "timestamp": "2026-05-12T14:23:45.123Z",
        "excerpt": "...we need to fix the [kafka] consumer group offset issue.",
        "project": "/Users/dev/develop/uu-rolecapacity-bff",
        "role": "assistant",
        "tool_origin": None,
    }
    base.update(overrides)
    return SearchHit(**base)  # type: ignore[arg-type]


def test_render_single_message_hit() -> None:
    hits = [_hit()]
    out = render_search_hits(
        query="kafka",
        hits=hits,
        total=1,
        config=SearchRenderConfig(),
    )
    assert "--- hit 1 of 1 ---" in out
    assert "session: 7e0ac728" in out
    assert "when:    2026-05-12 14:23:45Z" in out
    assert "kind:    message (assistant)" in out
    assert "content:" in out


def test_render_tool_call_uses_command_label() -> None:
    hits = [
        _hit(
            kind="tool_call",
            role=None,
            tool_origin="Bash",
            excerpt="kubectl logs -f deployment/[kafka]-consumer",
        )
    ]
    out = render_search_hits(
        query="kafka",
        hits=hits,
        total=1,
        config=SearchRenderConfig(),
    )
    assert "command:" in out
    assert "kind:    tool_call (Bash)" in out


def test_render_tool_result_uses_output_label() -> None:
    hits = [
        _hit(
            kind="tool_result",
            role=None,
            tool_origin="Read",
            excerpt="const KAFKA_TOPIC = '[kafka]_jta_dlq';",
        )
    ]
    out = render_search_hits(
        query="kafka",
        hits=hits,
        total=1,
        config=SearchRenderConfig(),
    )
    assert "output:" in out
    assert "kind:    tool_result (from Read)" in out


def test_render_zero_hits_includes_suggestions() -> None:
    out = render_search_hits(
        query="nonexistent",
        hits=[],
        total=0,
        config=SearchRenderConfig(),
        suggestions=[("widen_since", "widen --since from 7d to 30d")],
    )
    assert "0 hits" in out
    assert "widen --since from 7d to 30d" in out


def test_render_footer_includes_next_step_template() -> None:
    out = render_search_hits(
        query="kafka",
        hits=[_hit()],
        total=1,
        config=SearchRenderConfig(),
    )
    assert "convo inspect <session>" in out


def test_render_fields_projection() -> None:
    hits = [
        _hit(),
        _hit(
            session_id="ac6a97b8-1234-5678-9abc-def012345678",
            kind="tool_call",
            role=None,
            tool_origin="Bash",
        ),
    ]
    out = render_search_hits(
        query="kafka",
        hits=hits,
        total=2,
        config=SearchRenderConfig(fields=["session", "kind"]),
    )
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert any("7e0ac728" in ln and "message" in ln for ln in lines)
    assert any("ac6a97b8" in ln and "tool_call" in ln for ln in lines)
    # No section dividers in projection mode
    assert "--- hit" not in out
