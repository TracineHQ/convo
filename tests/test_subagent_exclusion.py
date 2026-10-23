"""Subagent sessions and transcript files stay out of counts unless --include-subagents is given.

Tool-call totals are work totals and always include subagent work.
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from convo.analytics import (
    compute_diff,
    gather_summary,
    stats_commands,
    stats_files,
    stats_model,
    stats_sessions,
    stats_tools,
)
from convo.cli import main
from convo.read.filters import ProjectResolveError, resolve_project_path
from convo.read.info import gather_info
from convo.read.projects import list_projects
from convo.read.sessions import list_sessions
from tests._seed import seed_message, seed_source_file

if TYPE_CHECKING:
    from pathlib import Path

    from convo.db import Database

_REPO = "/work/repo"
_OTHER = "/work/other"
_WORKTREE = "/work/repo/.claude/worktrees/wf_demo"

_TOP = {"top1", "top2"}
_SUBAGENTS = {"ag1.top1", "ag2.top1"}
_ALL = _TOP | _SUBAGENTS


def _ts(offset: timedelta) -> str:
    return (datetime.now(UTC) - offset).strftime("%Y-%m-%dT%H:%M:%SZ")


_Row = tuple[str, str, str, int, str, str]  # id, project, model, seconds, prompt, tool


def _add_sessions(db: Database, rows: list[_Row], *, started_ago: timedelta) -> None:
    """Insert sessions that started `started_ago` ago.

    Each owns a source file, a first user message and one tool call.
    """
    assert db.conn is not None
    start = _ts(started_ago)
    for sid, project, model, seconds, prompt, tool in rows:
        sfid = seed_source_file(db, path=f"/data/{sid}.jsonl")
        started = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        ended = (started + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at, ended_at, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, sfid, project, start, ended, model),
        )
        mid = f"m-{sid}"
        seed_message(db, sid, mid=mid, content=prompt, timestamp=start)
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, started_at) "
            "VALUES (?, ?, ?, 0, ?, '{}', ?)",
            (f"tc-{sid}", mid, sid, tool, start),
        )
    db.conn.commit()


@pytest.fixture
def populated(db: Database) -> Database:
    """Two top-level sessions plus two subagents of `top1`, one in its own worktree.

    Durations: top1 60s, top2 120s, ag1 20s, ag2 10s. All started two days ago, so
    they sit in `diff`'s current window.
    """
    _add_sessions(
        db,
        [
            ("top1", _REPO, "opus-4", 60, "build it", "Bash"),
            ("top2", _OTHER, "sonnet-4", 120, "build it", "Read"),
            ("ag1.top1", _REPO, "haiku-4", 20, "trace the rules", "Read"),
            ("ag2.top1", _WORKTREE, "haiku-4", 10, "trace the rules", "Read"),
        ],
        started_ago=timedelta(days=2),
    )
    return db


@pytest.fixture
def with_history(populated: Database) -> Database:
    """`populated` plus two top-level sessions and a subagent in `diff`'s previous window.

    Durations: top3 30s, top4 50s, ag3 5s, all nine days ago.
    """
    _add_sessions(
        populated,
        [
            ("top3", _REPO, "opus-4", 30, "ship it", "Bash"),
            ("top4", _OTHER, "opus-4", 50, "ship it", "Bash"),
            ("ag3.top3", _REPO, "haiku-4", 5, "scout", "Read"),
        ],
        started_ago=timedelta(days=9),
    )
    return populated


def _run(
    argv: list[str],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    monkeypatch.setenv("CONVO_DB", str(db_path))
    rc = main(argv)
    return rc, capsys.readouterr().out


def _json(
    argv: list[str],
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    rc, out = _run([*argv, "--json"], db_path, monkeypatch, capsys)
    assert rc == 0
    parsed: dict[str, Any] = json.loads(out)
    return parsed


# --------------------------------------------------------------------------- read helpers


def test_list_sessions_leaves_subagents_out_by_default(populated: Database) -> None:
    ids = {s.id for s in list_sessions(str(populated.path))}
    assert ids == _TOP


def test_list_sessions_includes_subagents_on_request(populated: Database) -> None:
    ids = {s.id for s in list_sessions(str(populated.path), include_subagents=True)}
    assert ids == _ALL


def test_list_projects_counts_top_level_sessions_by_default(populated: Database) -> None:
    counts = {p.path: p.sessions for p in list_projects(str(populated.path))}
    assert counts == {_REPO: 1, _OTHER: 1}


def test_list_projects_counts_subagents_on_request(populated: Database) -> None:
    counts = {
        p.path: p.sessions for p in list_projects(str(populated.path), include_subagents=True)
    }
    assert counts == {_REPO: 2, _OTHER: 1, _WORKTREE: 1}


def test_info_top_projects_skip_subagents_but_row_counts_stay_raw(populated: Database) -> None:
    report = gather_info(populated)
    assert {p.project_path: p.session_count for p in report.top_projects} == {_REPO: 1, _OTHER: 1}
    assert report.row_counts["sessions"] == len(_ALL)


# --------------------------------------------------------------------------- --project


def test_project_substring_resolves_among_top_level_sessions(populated: Database) -> None:
    assert populated.conn is not None
    assert resolve_project_path(populated.conn, "/work/re") == _REPO


def test_project_substring_is_ambiguous_once_subagents_count(populated: Database) -> None:
    assert populated.conn is not None
    with pytest.raises(ProjectResolveError, match="ambiguous"):
        resolve_project_path(populated.conn, "/work/re", include_subagents=True)


def test_subagent_only_project_needs_the_flag(populated: Database) -> None:
    assert populated.conn is not None
    with pytest.raises(ProjectResolveError, match="matched no known project"):
        resolve_project_path(populated.conn, "wf_demo")
    assert resolve_project_path(populated.conn, "wf_demo", include_subagents=True) == _WORKTREE


def test_exact_project_path_only_subagents_used_needs_the_flag(populated: Database) -> None:
    assert populated.conn is not None
    with pytest.raises(ProjectResolveError, match="matched no known project"):
        resolve_project_path(populated.conn, _WORKTREE)
    assert resolve_project_path(populated.conn, _WORKTREE, include_subagents=True) == _WORKTREE


# --------------------------------------------------------------------------- stats families


def test_stats_sessions_counts_top_level_sessions_by_default(populated: Database) -> None:
    report = stats_sessions(populated)
    assert report.total == 2
    assert report.median_duration_s is not None
    assert abs(report.median_duration_s - 90.0) < 0.01


def test_stats_sessions_counts_subagents_on_request(populated: Database) -> None:
    report = stats_sessions(populated, include_subagents=True)
    assert report.total == 4
    assert report.median_duration_s is not None
    assert abs(report.median_duration_s - 40.0) < 0.01


def test_stats_model_leaves_subagent_models_out_by_default(populated: Database) -> None:
    default = stats_model(populated)
    assert {m.model: m.session_count for m in default.by_model} == {"opus-4": 1, "sonnet-4": 1}
    with_sub = stats_model(populated, include_subagents=True)
    assert {m.model: m.session_count for m in with_sub.by_model} == {
        "opus-4": 1,
        "sonnet-4": 1,
        "haiku-4": 2,
    }


def test_stats_commands_skips_subagent_prompts_by_default(populated: Database) -> None:
    default = stats_commands(populated)
    assert {c.command: c.count for c in default.top_commands} == {"build it": 2}
    with_sub = stats_commands(populated, include_subagents=True)
    assert {c.command: c.count for c in with_sub.top_commands} == {
        "build it": 2,
        "trace the rules": 2,
    }


def test_stats_tools_always_include_subagent_work(populated: Database) -> None:
    assert stats_tools(populated).total == 4
    assert stats_tools(populated, project=_REPO).total == 2  # top1 Bash + ag1 Read


def test_stats_files_leave_subagent_transcripts_out_by_default(populated: Database) -> None:
    default = stats_files(populated)
    assert default.total == 2
    assert {f.path for f in default.top_files} == {"/data/top1.jsonl", "/data/top2.jsonl"}
    assert stats_files(populated, project=_REPO).total == 1
    assert stats_files(populated, since=timedelta(days=7)).total == 2


def test_stats_files_count_subagent_transcripts_on_request(populated: Database) -> None:
    assert stats_files(populated, include_subagents=True).total == 4
    assert stats_files(populated, project=_REPO, include_subagents=True).total == 2
    assert stats_files(populated, since=timedelta(days=7), include_subagents=True).total == 4


def test_stats_files_keep_the_guard_log_either_way(populated: Database) -> None:
    assert populated.conn is not None
    populated.conn.execute(
        "INSERT INTO source_files(path, size, mtime_ns, last_indexed_at, kind) "
        "VALUES ('/data/guard.jsonl', 10, 0, '2026-04-29T00:00:00Z', 'guard_decisions')"
    )
    populated.conn.commit()
    assert stats_files(populated).total == 3
    assert stats_files(populated, include_subagents=True).total == 5


# --------------------------------------------------------------------------- summary and diff


def test_summary_splits_session_counts_from_work_totals(populated: Database) -> None:
    default = gather_summary(populated)
    assert default.sessions.total == 2
    assert default.commands.total == 2
    assert sum(m.session_count for m in default.model.by_model) == 2
    assert default.tools.total == 4
    assert default.files.total == 2

    with_sub = gather_summary(populated, include_subagents=True)
    assert with_sub.sessions.total == 4
    assert with_sub.commands.total == 4
    assert sum(m.session_count for m in with_sub.model.by_model) == 4
    assert with_sub.tools.total == 4
    assert with_sub.files.total == 4


def test_diff_splits_session_counts_from_work_totals(populated: Database) -> None:
    default = compute_diff(populated).current
    assert default.sessions_count == 2
    assert default.commands_total == 2
    assert default.model_histogram == {"opus-4": 1, "sonnet-4": 1}
    assert default.sessions_median_seconds is not None
    assert abs(default.sessions_median_seconds - 90.0) < 0.01
    assert default.tool_calls_total == 4
    assert default.files_count == 2

    with_sub = compute_diff(populated, include_subagents=True).current
    assert with_sub.sessions_count == 4
    assert with_sub.commands_total == 4
    assert with_sub.model_histogram == {"opus-4": 1, "sonnet-4": 1, "haiku-4": 2}
    assert with_sub.sessions_median_seconds is not None
    assert abs(with_sub.sessions_median_seconds - 40.0) < 0.01
    assert with_sub.tool_calls_total == 4
    assert with_sub.files_count == 4


def _p95(seconds: list[float]) -> float:
    return statistics.quantiles(seconds, n=20, method="exclusive")[18]


def test_diff_current_p95_follows_the_flag(populated: Database) -> None:
    default = compute_diff(populated).current.sessions_p95_seconds
    assert default is not None
    assert abs(default - _p95([60.0, 120.0])) < 0.5
    with_sub = compute_diff(populated, include_subagents=True).current.sessions_p95_seconds
    assert with_sub is not None
    assert abs(with_sub - _p95([60.0, 120.0, 20.0, 10.0])) < 0.5


def test_diff_previous_window_follows_the_flag(with_history: Database) -> None:
    default = compute_diff(with_history).previous
    assert default.sessions_count == 2
    assert default.commands_total == 2
    assert default.model_histogram == {"opus-4": 2}
    assert default.files_count == 2
    assert default.tool_calls_total == 3
    assert default.sessions_median_seconds is not None
    assert abs(default.sessions_median_seconds - 40.0) < 0.01
    assert default.sessions_p95_seconds is not None
    assert abs(default.sessions_p95_seconds - _p95([30.0, 50.0])) < 0.5

    with_sub = compute_diff(with_history, include_subagents=True).previous
    assert with_sub.sessions_count == 3
    assert with_sub.commands_total == 3
    assert with_sub.model_histogram == {"opus-4": 2, "haiku-4": 1}
    assert with_sub.files_count == 3
    assert with_sub.tool_calls_total == 3
    assert with_sub.sessions_median_seconds is not None
    assert abs(with_sub.sessions_median_seconds - 30.0) < 0.01
    assert with_sub.sessions_p95_seconds is not None
    assert abs(with_sub.sessions_p95_seconds - _p95([30.0, 50.0, 5.0])) < 0.5


def test_diff_windows_do_not_bleed_into_each_other(with_history: Database) -> None:
    report = compute_diff(with_history)
    assert report.current.sessions_count == 2
    assert report.previous.sessions_count == 2


# --------------------------------------------------------------------------- CLI


def test_cli_sessions_flag(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    default = _json(["sessions"], populated.path, monkeypatch, capsys)
    assert {s["id"] for s in default["sessions"]["items"]} == _TOP
    with_sub = _json(["sessions", "--include-subagents"], populated.path, monkeypatch, capsys)
    assert {s["id"] for s in with_sub["sessions"]["items"]} == _ALL


def test_cli_sessions_prose_keeps_columns_aligned_for_long_subagent_ids(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = "aaaaaaaa-0000-4000-8000-000000000001"
    long_id = f"a1b2c3d4e5f6a7b8c.{parent}"
    _add_sessions(
        populated,
        [(long_id, _REPO, "haiku-4", 5, "scout", "Read")],
        started_ago=timedelta(days=1),
    )
    rc, out = _run(["sessions", "--include-subagents"], populated.path, monkeypatch, capsys)
    assert rc == 0
    lines = out.splitlines()
    assert long_id in out
    column = lines[1].index("project")
    rows = lines[3:]
    assert len(rows) == len(_ALL) + 1
    assert all(row[column : column + len("/work/")] == "/work/" for row in rows)


def test_cli_projects_flag(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    default = _json(["projects"], populated.path, monkeypatch, capsys)
    assert {p["path"]: p["sessions"] for p in default["projects"]["items"]} == {_REPO: 1, _OTHER: 1}
    with_sub = _json(["projects", "--include-subagents"], populated.path, monkeypatch, capsys)
    assert {p["path"]: p["sessions"] for p in with_sub["projects"]["items"]} == {
        _REPO: 2,
        _OTHER: 1,
        _WORKTREE: 1,
    }


def test_cli_sessions_project_ambiguity_follows_the_flag(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _run(
        ["sessions", "--project", "/work/re", "--json"], populated.path, monkeypatch, capsys
    )
    assert rc == 0
    assert {s["id"] for s in json.loads(out)["sessions"]["items"]} == {"top1"}
    rc, _ = _run(
        ["sessions", "--project", "/work/re", "--include-subagents"],
        populated.path,
        monkeypatch,
        capsys,
    )
    assert rc == 1


@pytest.mark.parametrize("argv", [["stats", "sessions"], ["summary"], ["diff"]])
def test_cli_project_ambiguity_follows_the_flag_everywhere(
    populated: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    rc, _ = _run([*argv, "--project", "/work/re", "--json"], populated.path, monkeypatch, capsys)
    assert rc == 0
    rc, out = _run(
        [*argv, "--project", "/work/re", "--include-subagents", "--json"],
        populated.path,
        monkeypatch,
        capsys,
    )
    assert rc == 1
    assert "ambiguous" in json.loads(out)["error"]["message"]


@pytest.mark.parametrize(
    ("family", "default_total", "with_subagents_total"),
    [("sessions", 2, 4), ("commands", 2, 4), ("model", 2, 4), ("files", 2, 4)],
)
def test_cli_stats_flag_changes_session_counts(
    populated: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    family: str,
    default_total: int,
    with_subagents_total: int,
) -> None:
    default = _json(["stats", family], populated.path, monkeypatch, capsys)
    assert default["stats"]["total"] == default_total
    with_sub = _json(["stats", family, "--include-subagents"], populated.path, monkeypatch, capsys)
    assert with_sub["stats"]["total"] == with_subagents_total


def test_cli_stats_tools_ignores_the_flag(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    default = _json(["stats", "tools"], populated.path, monkeypatch, capsys)
    with_sub = _json(["stats", "tools", "--include-subagents"], populated.path, monkeypatch, capsys)
    assert default["stats"]["total"] == with_sub["stats"]["total"] == 4


def test_cli_stats_hooks_accepts_the_flag(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(
        ["stats", "hooks", "--include-subagents", "--json"], populated.path, monkeypatch, capsys
    )
    assert rc == 0


def test_cli_summary_flag(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    default = _json(["summary"], populated.path, monkeypatch, capsys)["summary"]
    assert default["sessions"]["total"] == 2
    assert default["files"]["total"] == 2
    assert default["tools"]["total"] == 4
    with_sub = _json(["summary", "--include-subagents"], populated.path, monkeypatch, capsys)
    assert with_sub["summary"]["sessions"]["total"] == 4
    assert with_sub["summary"]["files"]["total"] == 4
    assert with_sub["summary"]["tools"]["total"] == 4


def test_cli_diff_flag(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    default = _json(["diff"], populated.path, monkeypatch, capsys)["diff"]["current"]
    assert default["sessions_count"] == 2
    assert default["files_count"] == 2
    assert default["tool_calls_total"] == 4
    with_sub = _json(["diff", "--include-subagents"], populated.path, monkeypatch, capsys)
    assert with_sub["diff"]["current"]["sessions_count"] == 4
    assert with_sub["diff"]["current"]["files_count"] == 4
    assert with_sub["diff"]["current"]["tool_calls_total"] == 4


def test_cli_stats_project_filter_keeps_subagent_tool_calls(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _json(["stats", "tools", "--project", "repo"], populated.path, monkeypatch, capsys)
    assert out["stats"]["total"] == 2
