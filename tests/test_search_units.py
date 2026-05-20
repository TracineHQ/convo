"""Service-isolated unit tests for `convo.read.search` SQL composition.

These tests pin the SQL strings the builders generate and the order of bound
params. Refactors that change query shape will fail loudly. Complementary to
`test_read_search.py` (integration) — here we never touch a real DB.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, cast

import pytest

from convo.read.search import (
    _Filters,
    _message_branch,
    _run_search,
    _tool_call_branch,
    _tool_result_branch,
    build_fts_query,
)

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture


# --------------------------------------------------------------------------- build_fts_query


class TestBuildFtsQuery:
    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            build_fts_query("")

    def test_whitespace_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            build_fts_query("   \t\n ")

    def test_single_word_phrase_mode(self) -> None:
        assert build_fts_query("hello") == '"hello"'

    def test_single_word_too_short_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 3 characters"):
            build_fts_query("hi")

    def test_multiword_phrase_mode_wraps_whole(self) -> None:
        # v2: was phrase-default, now AND-default — each token becomes a separate phrase.
        assert build_fts_query("hello world") == '"hello" "world"'

    def test_required_token(self) -> None:
        assert build_fts_query("+token") == '"token"'

    def test_negated_token(self) -> None:
        assert build_fts_query("-token") == 'NOT "token"'

    def test_mixed_operators_preserve_order(self) -> None:
        assert build_fts_query("+alpha -beta") == '"alpha" NOT "beta"'

    def test_operator_short_term_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 3 characters"):
            build_fts_query("+ab -other")

    def test_quote_in_phrase_mode_escaped(self) -> None:
        # v2: was phrase-default, now AND-default — quoted span becomes its own PHRASE token.
        # 'say "hello" loud' → "say" "hello" "loud" (each AND'd together).
        assert build_fts_query('say "hello" loud') == '"say" "hello" "loud"'

    def test_quote_in_operator_mode_escaped(self) -> None:
        # Quotes inside an operator token are still escaped.
        assert build_fts_query('+ab"cd') == '"ab""cd"'


# --------------------------------------------------------------------------- helpers


def _make_filters(
    *,
    fts_match: str = '"foo"',
    since_iso: str | None = None,
    project: str | None = None,
    tool: str | None = None,
    session: str | None = None,
    tool_exact: bool = False,
    snippet_tokens: int = 12,
) -> _Filters:
    return _Filters(
        fts_match=fts_match,
        since_iso=since_iso,
        project=project,
        tool=tool,
        session=session,
        tool_exact=tool_exact,
        snippet_tokens=snippet_tokens,
    )


# --------------------------------------------------------------------------- branch builders


class TestBranchBuilders:
    def test_message_branch_minimal(self) -> None:
        sql, params = _message_branch(_make_filters())
        assert "FROM messages_fts" in sql
        assert "JOIN messages m ON m.rowid = messages_fts.rowid" in sql
        assert "JOIN sessions s ON s.id = m.session_id" in sql
        assert "messages_fts MATCH ?" in sql
        assert "'message' AS kind" in sql
        assert params == ['"foo"']

    def test_message_branch_with_since_and_project(self) -> None:
        sql, params = _message_branch(
            _make_filters(since_iso="2024-01-01T00:00:00.000Z", project="/proj/x")
        )
        assert "m.timestamp IS NOT NULL AND m.timestamp >= ?" in sql
        assert "s.project_path = ?" in sql
        assert params == ['"foo"', "2024-01-01T00:00:00.000Z", "/proj/x"]

    def test_tool_call_branch_minimal(self) -> None:
        sql, params = _tool_call_branch(_make_filters())
        assert "FROM tool_calls_fts" in sql
        assert "JOIN tool_calls tc ON tc.rowid = tool_calls_fts.rowid" in sql
        assert "JOIN sessions s ON s.id = tc.session_id" in sql
        assert "tool_calls_fts MATCH ?" in sql
        assert "'tool_call' AS kind" in sql
        assert params == ['"foo"']

    def test_tool_call_branch_with_all_filters(self) -> None:
        sql, params = _tool_call_branch(
            _make_filters(
                since_iso="2024-01-01T00:00:00.000Z",
                project="/proj/x",
                tool="Bash",
            )
        )
        assert "tc.started_at IS NOT NULL AND tc.started_at >= ?" in sql
        assert "s.project_path = ?" in sql
        assert "tc.name LIKE ?" in sql
        assert params == ['"foo"', "2024-01-01T00:00:00.000Z", "/proj/x", "Bash%"]

    def test_tool_call_branch_tool_exact(self) -> None:
        sql, params = _tool_call_branch(_make_filters(tool="Bash", tool_exact=True))
        assert "tc.name = ?" in sql
        assert params == ['"foo"', "Bash"]

    def test_tool_result_branch_minimal(self) -> None:
        sql, params = _tool_result_branch(_make_filters())
        assert "FROM tool_results_fts" in sql
        assert "JOIN tool_results tr ON tr.rowid = tool_results_fts.rowid" in sql
        assert "JOIN tool_calls tc ON tc.id = tr.tool_call_id" in sql
        assert "JOIN sessions s ON s.id = tc.session_id" in sql
        assert "LEFT JOIN messages m ON m.id = tr.message_id" in sql
        assert "tool_results_fts MATCH ?" in sql
        assert "'tool_result' AS kind" in sql
        assert params == ['"foo"']

    def test_tool_result_branch_with_all_filters(self) -> None:
        sql, params = _tool_result_branch(
            _make_filters(
                since_iso="2024-01-01T00:00:00.000Z",
                project="/proj/x",
                tool="Bash",
            )
        )
        assert "m.timestamp IS NOT NULL AND m.timestamp >= ?" in sql
        assert "s.project_path = ?" in sql
        assert "tc.name LIKE ?" in sql
        assert params == ['"foo"', "2024-01-01T00:00:00.000Z", "/proj/x", "Bash%"]

    def test_tool_result_branch_tool_exact(self) -> None:
        sql, params = _tool_result_branch(_make_filters(tool="Bash", tool_exact=True))
        assert "tc.name = ?" in sql
        assert params == ['"foo"', "Bash"]


# --------------------------------------------------------------------------- _run_search


def _stub_conn(mocker: MockerFixture) -> tuple[sqlite3.Connection, MagicMock]:
    """Build an autospec'd `sqlite3.Connection` whose `execute` returns no rows.

    Returns the typed Connection (for passing to the SUT) plus the underlying
    MagicMock (for `.call_args` introspection without fighting mypy).
    """
    conn = mocker.create_autospec(sqlite3.Connection, spec_set=True, instance=True)
    # `execute()` returns a Cursor in real life; for `_run_search` we only iterate
    # the result, so any iterable suffices.
    conn.execute.return_value = iter([])
    return cast("sqlite3.Connection", conn), conn


class TestRunSearchComposition:
    def test_no_tool_filter_three_branches(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        result = _run_search(conn, _make_filters(), limit=50)
        assert result == []
        sql, params = mock.execute.call_args.args
        # Three SELECT branches joined with UNION ALL.
        assert sql.count("UNION ALL") == 2
        assert "FROM messages_fts" in sql
        assert "FROM tool_calls_fts" in sql
        assert "FROM tool_results_fts" in sql
        # Params: one MATCH per branch + final LIMIT.
        assert params == ['"foo"', '"foo"', '"foo"', 50]
        assert sql.rstrip().endswith("LIMIT ?")

    def test_with_tool_filter_two_branches(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        _run_search(conn, _make_filters(tool="Bash"), limit=25)
        sql, params = mock.execute.call_args.args
        # message branch is skipped when tool is set.
        assert sql.count("UNION ALL") == 1
        assert "FROM messages_fts" not in sql
        assert "FROM tool_calls_fts" in sql
        assert "FROM tool_results_fts" in sql
        # 2 branches with (match + tool LIKE) each + LIMIT.
        assert params == ['"foo"', "Bash%", '"foo"', "Bash%", 25]

    def test_since_iso_applies_to_all_three_branches(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        _run_search(
            conn,
            _make_filters(since_iso="2024-01-01T00:00:00.000Z"),
            limit=10,
        )
        sql, params = mock.execute.call_args.args
        # Two timestamp filters bind to messages and tool_results' message join,
        # plus tool_calls' started_at filter — three since binds total.
        assert params.count("2024-01-01T00:00:00.000Z") == 3
        assert sql.count("m.timestamp IS NOT NULL AND m.timestamp >= ?") == 2
        assert sql.count("tc.started_at IS NOT NULL AND tc.started_at >= ?") == 1
        assert params[-1] == 10

    def test_project_applies_to_all_three_branches(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        _run_search(conn, _make_filters(project="/proj/x"), limit=5)
        sql, params = mock.execute.call_args.args
        assert sql.count("s.project_path = ?") == 3
        assert params.count("/proj/x") == 3
        assert params[-1] == 5

    def test_tool_filter_only_in_tool_branches(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        _run_search(conn, _make_filters(tool="Edit"), limit=5)
        sql, _ = mock.execute.call_args.args
        # `tc.name LIKE ?` appears once per tool-related branch (tool_calls + tool_results).
        assert sql.count("tc.name LIKE ?") == 2

    def test_limit_param_bound_last(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        _run_search(conn, _make_filters(), limit=99)
        _, params = mock.execute.call_args.args
        assert params[-1] == 99
        assert isinstance(params[-1], int)
