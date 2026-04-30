"""Tests for `gather_summary` analytics composition."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from convo.analytics import gather_summary
from tests._seed import seed_message, seed_source_file

if TYPE_CHECKING:
    from convo.db import Database


def _populate(db: Database) -> None:
    """Seed a populated DB usable across all five families.

    Two sessions on different projects/models/timestamps so that since/project
    filters narrow consistently across all sub-reports.
    """
    sfid_a = seed_source_file(db, path="/data/a.jsonl")
    sfid_b = seed_source_file(db, path="/data/b.jsonl")
    assert db.conn is not None
    # s1 on project A, old, opus
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at, "
        "ended_at, model) VALUES (?, ?, ?, ?, ?, ?)",
        ("s1", sfid_a, "/proj/A", "2020-01-01T00:00:00Z", "2020-01-01T00:00:30Z", "opus-4"),
    )
    # s2 on project B, recent, sonnet
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at, "
        "ended_at, model) VALUES (?, ?, ?, ?, ?, ?)",
        ("s2", sfid_b, "/proj/B", "2999-01-01T00:00:00Z", "2999-01-01T00:00:10Z", "sonnet-4"),
    )
    db.conn.commit()
    seed_message(db, "s1", mid="m1", content="run the build A")
    seed_message(db, "s2", mid="m2", content="run the build B")
    # Set message timestamps so since-filters can include them.
    db.conn.execute("UPDATE messages SET timestamp = ? WHERE id = 'm1'", ("2020-01-01T00:00:00Z",))
    db.conn.execute("UPDATE messages SET timestamp = ? WHERE id = 'm2'", ("2999-01-01T00:00:00Z",))
    db.conn.commit()
    # tool calls: 3 Bash on s1, 2 Read on s2
    rows: list[tuple[str, str, str, int, str, str]] = [
        *[(f"tcb{i}", "m1", "s1", i, "Bash", "2020-01-01T00:00:00Z") for i in range(3)],
        *[(f"tcr{i}", "m2", "s2", i, "Read", "2999-01-01T00:00:00Z") for i in range(2)],
    ]
    for tcid, mid, sid, seq, name, ts in rows:
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, "
            "input_json, started_at, duration_ms) VALUES (?, ?, ?, ?, ?, '{}', ?, 100)",
            (tcid, mid, sid, seq, name, ts),
        )
    # one error result
    db.conn.execute(
        "INSERT INTO tool_results(tool_call_id, is_error, output_text) VALUES ('tcb0', 1, 'oops')",
    )
    db.conn.execute(
        "INSERT INTO tool_results(tool_call_id, is_error, output_text) VALUES ('tcr0', 0, 'ok')",
    )
    # source_files message_count
    db.conn.execute("UPDATE source_files SET message_count = 1 WHERE id = ?", (sfid_a,))
    db.conn.execute("UPDATE source_files SET message_count = 1 WHERE id = ?", (sfid_b,))
    db.conn.commit()


def test_gather_summary_all_subreports_populated(db: Database) -> None:
    _populate(db)
    report = gather_summary(db)
    assert report.since is None
    assert report.project is None
    # tools
    assert report.tools.total_calls == 5
    names = {f.name for f in report.tools.top_by_frequency}
    assert names == {"Bash", "Read"}
    # commands
    assert report.commands.total_sessions_with_command == 2
    assert {c.command for c in report.commands.top_commands} == {
        "run the build A",
        "run the build B",
    }
    # sessions
    assert report.sessions.total_sessions == 2
    assert report.sessions.sessions_with_duration == 2
    # files
    assert report.files.total_files == 2
    assert report.files.total_message_count == 2
    # model
    assert report.model.total_sessions == 2
    assert {m.model for m in report.model.by_model} == {"opus-4", "sonnet-4"}


def test_gather_summary_since_filter_narrows_all_subreports(db: Database) -> None:
    _populate(db)
    # since=1d drops anything from 2020 (old s1)
    report = gather_summary(db, since=timedelta(days=1))
    # tools: only Read remains
    assert report.tools.total_calls == 2
    assert {f.name for f in report.tools.top_by_frequency} == {"Read"}
    # commands: only s2's command
    assert report.commands.total_sessions_with_command == 1
    assert report.commands.top_commands[0].command == "run the build B"
    # sessions: only s2
    assert report.sessions.total_sessions == 1
    # files: only the file linked to s2
    assert report.files.total_files == 1
    # model: only sonnet-4
    assert report.model.total_sessions == 1
    assert report.model.by_model[0].model == "sonnet-4"
    # `since` echoed in the report
    assert report.since == timedelta(days=1)


def test_gather_summary_project_filter_narrows_all_subreports(db: Database) -> None:
    _populate(db)
    report = gather_summary(db, project="/proj/B")
    assert report.tools.total_calls == 2
    assert {f.name for f in report.tools.top_by_frequency} == {"Read"}
    assert report.commands.total_sessions_with_command == 1
    assert report.sessions.total_sessions == 1
    assert report.files.total_files == 1
    assert report.model.total_sessions == 1
    assert report.project == "/proj/B"


def test_gather_summary_empty_db(db: Database) -> None:
    report = gather_summary(db)
    assert report.tools.total_calls == 0
    assert report.tools.top_by_frequency == ()
    assert report.commands.total_sessions_with_command == 0
    assert report.commands.top_commands == ()
    assert report.sessions.total_sessions == 0
    assert report.sessions.median_duration_s is None
    assert report.files.total_files == 0
    assert report.files.top_files == ()
    assert report.model.total_sessions == 0
    assert report.model.by_model == ()
