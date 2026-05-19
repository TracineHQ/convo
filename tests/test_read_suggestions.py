"""Tests for `generate_suggestions` in `convo.read.suggestions`."""

from __future__ import annotations

import pytest

from convo.read.suggestions import Suggestion, generate_suggestions


def test_hyphenated_query_suggests_split() -> None:
    suggestions = generate_suggestions(
        query="cross-examine",
        since_span="7d",
        project=None,
        tool=None,
        session=None,
        total_doc_freq={"cross": 100, "examine": 20},
    )
    kinds = [s.kind for s in suggestions]
    assert "split_hyphens" in kinds


def test_since_set_suggests_widen() -> None:
    suggestions = generate_suggestions(
        query="kafka",
        since_span="7d",
        project=None,
        tool=None,
        session=None,
        total_doc_freq={"kafka": 0},
    )
    widen = [s for s in suggestions if s.kind == "widen_since"]
    assert len(widen) == 1
    assert widen[0].to_value == "30d"


def test_project_set_suggests_drop() -> None:
    suggestions = generate_suggestions(
        query="kafka",
        since_span=None,
        project="tracine-ops",
        tool=None,
        session=None,
        total_doc_freq={"kafka": 0},
    )
    drops = [s for s in suggestions if s.kind == "drop_project"]
    assert len(drops) == 1


def test_three_token_query_suggests_drop_rarest() -> None:
    suggestions = generate_suggestions(
        query="kafka migration retry",
        since_span=None,
        project=None,
        tool=None,
        session=None,
        total_doc_freq={"kafka": 100, "migration": 50, "retry": 5},
    )
    drop_rare = [s for s in suggestions if s.kind == "drop_rare_token"]
    assert len(drop_rare) == 1
    assert drop_rare[0].drop_token == "retry"  # noqa: S105 # lowest doc-freq


def test_no_filters_no_suggestions() -> None:
    suggestions = generate_suggestions(
        query="kafka",
        since_span=None,
        project=None,
        tool=None,
        session=None,
        total_doc_freq={"kafka": 0},
    )
    assert suggestions == []


def test_suggestion_dataclass_is_frozen() -> None:
    s = Suggestion(kind="widen_since", description="x")
    with pytest.raises((AttributeError, TypeError)):
        s.kind = "other"  # type: ignore[misc]
