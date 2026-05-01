"""Service-isolated unit tests for analytics SQL composition.

Pins the SQL strings and parameter binding for `stats_tools`, `stats_sessions`,
and `diff` query builders. Complementary to the integration tests, which only
verify end-to-end results — these tests fail loudly on shape/binding regressions.
"""

from __future__ import annotations

import importlib
import sqlite3
from typing import TYPE_CHECKING, cast

from convo.analytics import diff
from convo.analytics._constants import SECONDS_PER_DAY

# `from convo.analytics import stats_tools` would shadow the submodule with the
# re-exported function of the same name in `convo.analytics.__init__`. Resolve
# the submodule explicitly to access its private helpers.
stats_tools_mod = importlib.import_module("convo.analytics.stats_tools")
stats_sessions_mod = importlib.import_module("convo.analytics.stats_sessions")

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from pytest_mock import MockerFixture


# ---- helpers


def _stub_conn(mocker: MockerFixture) -> tuple[sqlite3.Connection, MagicMock]:
    """Build an autospec'd Connection whose `execute()` returns a stubbed Cursor."""
    conn = mocker.create_autospec(sqlite3.Connection, spec_set=True, instance=True)
    cursor = mocker.create_autospec(sqlite3.Cursor, spec_set=True, instance=True)
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None
    conn.execute.return_value = cursor
    return cast("sqlite3.Connection", conn), conn


# ---- stats_tools._where_and_params


class TestWhereAndParams:
    def test_no_filters(self) -> None:
        where_sql, params, needs_join = stats_tools_mod._where_and_params(cutoff=None, project=None)
        assert where_sql == ""
        assert params == []
        assert needs_join is False

    def test_only_cutoff(self) -> None:
        where_sql, params, needs_join = stats_tools_mod._where_and_params(
            cutoff="2024-01-01T00:00:00.000Z", project=None
        )
        assert where_sql == " WHERE tc.started_at IS NOT NULL AND tc.started_at >= ?"
        assert params == ["2024-01-01T00:00:00.000Z"]
        assert needs_join is False

    def test_only_project(self) -> None:
        where_sql, params, needs_join = stats_tools_mod._where_and_params(
            cutoff=None, project="/proj/x"
        )
        assert where_sql == " WHERE s.project_path = ?"
        assert params == ["/proj/x"]
        assert needs_join is True

    def test_both(self) -> None:
        where_sql, params, needs_join = stats_tools_mod._where_and_params(
            cutoff="2024-01-01T00:00:00.000Z", project="/proj/x"
        )
        assert (
            where_sql
            == " WHERE tc.started_at IS NOT NULL AND tc.started_at >= ? AND s.project_path = ?"
        )
        # Param order matches WHERE clause order.
        assert params == ["2024-01-01T00:00:00.000Z", "/proj/x"]
        assert needs_join is True


# ---- stats_tools._top_by_frequency


class TestTopByFrequency:
    def test_sql_shape_and_limit_last(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        stats_tools_mod._top_by_frequency(conn, cutoff=None, project=None)
        sql, params = mock.execute.call_args.args
        assert "GROUP BY tc.name" in sql
        assert "ORDER BY n DESC, tc.name" in sql
        assert sql.rstrip().endswith("LIMIT ?")
        # No filters → only the LIMIT param.
        assert params == [stats_tools_mod._TOP_FREQ_LIMIT]

    def test_with_project_adds_join_and_param(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        stats_tools_mod._top_by_frequency(conn, cutoff=None, project="/proj/x")
        sql, params = mock.execute.call_args.args
        assert "JOIN sessions s ON s.id = tc.session_id" in sql
        assert params == ["/proj/x", stats_tools_mod._TOP_FREQ_LIMIT]

    def test_with_cutoff_no_join(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        stats_tools_mod._top_by_frequency(conn, cutoff="2024-01-01T00:00:00.000Z", project=None)
        sql, params = mock.execute.call_args.args
        assert "JOIN sessions" not in sql
        assert params == ["2024-01-01T00:00:00.000Z", stats_tools_mod._TOP_FREQ_LIMIT]


# ---- stats_tools._error_rates


class TestErrorRates:
    def test_left_join_tool_results(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        stats_tools_mod._error_rates(conn, cutoff=None, project=None)
        sql, _ = mock.execute.call_args.args
        # LEFT JOIN ensures calls without a recorded result are still counted.
        assert "LEFT JOIN tool_results tr ON tr.tool_call_id = tc.id" in sql
        assert "GROUP BY tc.name" in sql
        assert "SUM(COALESCE(tr.is_error, 0))" in sql

    def test_with_filters(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        stats_tools_mod._error_rates(conn, cutoff="2024-01-01T00:00:00.000Z", project="/proj/x")
        sql, params = mock.execute.call_args.args
        assert "JOIN sessions s ON s.id = tc.session_id" in sql
        assert "LEFT JOIN tool_results tr ON tr.tool_call_id = tc.id" in sql
        assert params == ["2024-01-01T00:00:00.000Z", "/proj/x"]


# ---- stats_sessions


class TestStatsSessions:
    def test_seconds_per_day_first_param(self, mocker: MockerFixture) -> None:
        # Patch open_ro so we drive a stub Connection without touching the DB.
        conn, mock = _stub_conn(mocker)
        mocker.patch.object(stats_sessions_mod, "open_ro", return_value=conn)
        # Pass a dummy db; only its `path` attribute is read by open_ro (which we stubbed).
        db = mocker.MagicMock()
        stats_sessions_mod.stats_sessions(db)
        sql, params = mock.execute.call_args.args
        # The recent fix: SECONDS_PER_DAY is bound, not f-stringed.
        assert "* ?" in sql
        assert params[0] == SECONDS_PER_DAY

    def test_with_filters_appends_after_seconds(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        mocker.patch.object(stats_sessions_mod, "open_ro", return_value=conn)
        # since_iso() with None returns None; pass project to verify ordering.
        db = mocker.MagicMock()
        stats_sessions_mod.stats_sessions(db, project="/proj/x")
        sql, params = mock.execute.call_args.args
        assert params[0] == SECONDS_PER_DAY
        assert "/proj/x" in params
        assert "project_path = ?" in sql


# ---- diff._sessions_durations


class TestSessionsDurations:
    def test_seconds_per_day_first_param(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        diff._sessions_durations(conn, "2024-01-01T00:00:00.000Z", "2024-01-08T00:00:00.000Z", None)
        sql, params = mock.execute.call_args.args
        assert "* ?" in sql
        assert params[0] == SECONDS_PER_DAY
        # Then lower, upper.
        assert params[1] == "2024-01-01T00:00:00.000Z"
        assert params[2] == "2024-01-08T00:00:00.000Z"

    def test_with_project_appends_param(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        diff._sessions_durations(
            conn, "2024-01-01T00:00:00.000Z", "2024-01-08T00:00:00.000Z", "/proj/x"
        )
        _, params = mock.execute.call_args.args
        assert params[0] == SECONDS_PER_DAY
        assert params[-1] == "/proj/x"


# ---- diff._tool_calls_total


class TestToolCallsTotal:
    def test_no_project_no_session_join(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        # fetchone returns a row-like tuple; default is None which yields 0.
        cursor = mock.execute.return_value
        cursor.fetchone.return_value = (0,)
        diff._tool_calls_total(conn, "2024-01-01T00:00:00.000Z", "2024-01-08T00:00:00.000Z", None)
        sql, params = mock.execute.call_args.args
        assert "JOIN sessions" not in sql
        assert "FROM tool_calls tc" in sql
        # Only lower/upper bind.
        assert params == ["2024-01-01T00:00:00.000Z", "2024-01-08T00:00:00.000Z"]

    def test_with_project_adds_join(self, mocker: MockerFixture) -> None:
        conn, mock = _stub_conn(mocker)
        cursor = mock.execute.return_value
        cursor.fetchone.return_value = (0,)
        diff._tool_calls_total(
            conn, "2024-01-01T00:00:00.000Z", "2024-01-08T00:00:00.000Z", "/proj/x"
        )
        sql, params = mock.execute.call_args.args
        assert "JOIN sessions s ON s.id = tc.session_id" in sql
        assert params == ["2024-01-01T00:00:00.000Z", "2024-01-08T00:00:00.000Z", "/proj/x"]
