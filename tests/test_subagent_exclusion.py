"""Subagent sessions stay out of session counts unless --include-subagents is given.

Tool-call and file totals are work totals and always include subagent work.
"""

from __future__ import annotations

import json
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


def _populate(db: Database) -> None:
    """Two top-level sessions plus two subagents of `top1`, one in its own worktree.

    Durations: top1 60s, top2 120s, ag1 20s, ag2 10s. Each session owns a source file,
    a first user message and one tool call (top1 Bash, the rest Read).
    """
    assert db.conn is not None
    start = _ts(timedelta(days=2))
    rows = [
        ("top1", _REPO, "opus-4", 60, "build it", "Bash"),
        ("top2", _OTHER, "sonnet-4", 120, "build it", "Read"),
        ("ag1.top1", _REPO, "haiku-4", 20, "trace the rules", "Read"),
        ("ag2.top1", _WORKTREE, "haiku-4", 10, "trace the rules", "Read"),
    ]
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
    _populate(db)
    return db


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


def test_stats_tools_and_files_always_include_subagent_work(populated: Database) -> None:
    assert stats_tools(populated).total == 4
    assert stats_tools(populated, project=_REPO).total == 2  # top1 Bash + ag1 Read
    assert stats_files(populated).total == 4
    assert stats_files(populated, project=_REPO).total == 2


# --------------------------------------------------------------------------- summary and diff


def test_summary_splits_session_counts_from_work_totals(populated: Database) -> None:
    default = gather_summary(populated)
    assert default.sessions.total == 2
    assert default.commands.total == 2
    assert sum(m.session_count for m in default.model.by_model) == 2
    assert default.tools.total == 4
    assert default.files.total == 4

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
    assert default.files_count == 4

    with_sub = compute_diff(populated, include_subagents=True).current
    assert with_sub.sessions_count == 4
    assert with_sub.commands_total == 4
    assert with_sub.model_histogram == {"opus-4": 1, "sonnet-4": 1, "haiku-4": 2}
    assert with_sub.sessions_median_seconds is not None
    assert abs(with_sub.sessions_median_seconds - 40.0) < 0.01
    assert with_sub.tool_calls_total == 4
    assert with_sub.files_count == 4


# --------------------------------------------------------------------------- CLI


def test_cli_sessions_flag(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    default = _json(["sessions"], populated.path, monkeypatch, capsys)
    assert {s["id"] for s in default["sessions"]["items"]} == _TOP
    with_sub = _json(["sessions", "--include-subagents"], populated.path, monkeypatch, capsys)
    assert {s["id"] for s in with_sub["sessions"]["items"]} == _ALL


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


@pytest.mark.parametrize(
    ("family", "default_total", "with_subagents_total"),
    [("sessions", 2, 4), ("commands", 2, 4), ("model", 2, 4)],
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


@pytest.mark.parametrize("family", ["tools", "files"])
def test_cli_stats_work_families_ignore_the_flag(
    populated: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    family: str,
) -> None:
    default = _json(["stats", family], populated.path, monkeypatch, capsys)
    with_sub = _json(["stats", family, "--include-subagents"], populated.path, monkeypatch, capsys)
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
    assert default["tools"]["total"] == 4
    with_sub = _json(["summary", "--include-subagents"], populated.path, monkeypatch, capsys)
    assert with_sub["summary"]["sessions"]["total"] == 4
    assert with_sub["summary"]["tools"]["total"] == 4


def test_cli_diff_flag(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    default = _json(["diff"], populated.path, monkeypatch, capsys)["diff"]["current"]
    assert default["sessions_count"] == 2
    assert default["tool_calls_total"] == 4
    with_sub = _json(["diff", "--include-subagents"], populated.path, monkeypatch, capsys)
    assert with_sub["diff"]["current"]["sessions_count"] == 4
    assert with_sub["diff"]["current"]["tool_calls_total"] == 4


def test_cli_stats_project_filter_keeps_subagent_tool_calls(
    populated: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _json(["stats", "tools", "--project", "repo"], populated.path, monkeypatch, capsys)
    assert out["stats"]["total"] == 2
