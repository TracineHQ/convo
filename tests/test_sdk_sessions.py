"""Session entrypoints: migration 0007, capture at index time, default SDK filtering."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from convo.cli import main
from convo.db import Database
from convo.intake.entrypoint import file_entrypoint, session_entrypoint
from convo.intake.orchestrator import index_file
from tests._seed import build_db_at_version, seed_guard_decision

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

_RECENT = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
_PROJECT = "/work/app"

# id -> entrypoint; one session of each kind the default filter has to sort.
_SESSIONS: dict[str, str | None] = {
    "s-cli": "cli",
    "s-unknown": None,
    "s-sdk-cli": "sdk-cli",
    "s-sdk-py": "sdk-py",
    "s-inferred": "sdk-inferred",
}
_INTERACTIVE = {"s-cli", "s-unknown"}


def _seed(conn: sqlite3.Connection, sid: str, entrypoint: str | None, n: int) -> None:
    """One session: `n` messages, a Bash call on the second, a guard decision."""
    cur = conn.execute(
        "INSERT INTO source_files(path, size, mtime_ns, last_indexed_at, message_count) "
        "VALUES (?, 1, 1, ?, ?)",
        (f"/gone/{sid}.jsonl", _RECENT, n),
    )
    conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at, ended_at, model, "
        "entrypoint) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, cur.lastrowid, _PROJECT, _RECENT, _RECENT, f"model-{sid}", entrypoint),
    )
    for i in range(n):
        conn.execute(
            "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, '{}')",
            (f"{sid}-m{i}", sid, "assistant" if i % 2 else "user", i, _RECENT, f"needle {sid}"),
        )
    conn.execute(
        "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, started_at) "
        "VALUES (?, ?, ?, 0, 'Bash', '{}', ?)",
        (f"{sid}-tc", f"{sid}-m1", sid, _RECENT),
    )
    conn.execute(
        "INSERT INTO tool_results(tool_call_id, message_id, output_text) VALUES (?, ?, ?)",
        (f"{sid}-tc", f"{sid}-m1", f"needle output {sid}"),
    )


@pytest.fixture
def mixed_db(db_path: Path) -> Path:
    with Database(db_path) as db:
        assert db.conn is not None
        for sid, entrypoint in _SESSIONS.items():
            _seed(db.conn, sid, entrypoint, 2)
        db.conn.commit()
        sfid = db.conn.execute("SELECT MIN(id) FROM source_files").fetchone()[0]
        for n, sid in enumerate(_SESSIONS, start=1):
            seed_guard_decision(db, sfid, line_no=n, session_id=sid, cwd=_PROJECT)
    return db_path


def _json(db: Path, argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    assert main(["--db", str(db), *argv]) == 0
    out: dict[str, object] = json.loads(capsys.readouterr().out)
    return out


# --------------------------------------------------------------------------- migration


def _raw(entrypoint: str | None) -> str:
    return json.dumps({"type": "user"} if entrypoint is None else {"entrypoint": entrypoint})


def test_migration_0007_backfills_from_stored_raw_json(db_path: Path) -> None:
    raw = build_db_at_version(db_path, 6)
    cases: dict[str, list[str]] = {
        "cli": [_raw("cli"), _raw("cli")],
        "mixed": [_raw(None), _raw("sdk-py"), _raw("cli")],
        "none": [_raw(None), _raw(None)],
        "bad-json": ["not json", _raw("sdk-cli")],
        "empty": [_raw("")],
    }
    for n, (sid, raws) in enumerate(cases.items(), start=1):
        raw.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (?, ?, 1, 1, ?)",
            (n, f"/x/{sid}.jsonl", _RECENT),
        )
        raw.execute("INSERT INTO sessions(id, source_file_id) VALUES (?, ?)", (sid, n))
        for seq, raw_json in enumerate(raws):
            raw.execute(
                "INSERT INTO messages(id, session_id, role, seq, raw_json) "
                "VALUES (?, ?, 'user', ?, ?)",
                (f"{sid}-{seq}", sid, seq, raw_json),
            )
    raw.commit()
    raw.close()

    with Database(db_path) as db:
        assert db.conn is not None
        assert db.schema_version() == 7
        got = dict(db.conn.execute("SELECT id, entrypoint FROM sessions").fetchall())
    assert got == {
        "cli": "cli",
        "mixed": "sdk-py",
        "none": None,
        "bad-json": "sdk-cli",
        "empty": None,
    }


def test_migration_0007_keeps_every_row(db_path: Path) -> None:
    raw = build_db_at_version(db_path, 6)
    raw.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (1, '/x.jsonl', 1, 1, ?)",
        (_RECENT,),
    )
    raw.execute("INSERT INTO sessions(id, source_file_id) VALUES ('s', 1)")
    raw.execute(
        "INSERT INTO messages(id, session_id, role, seq, raw_json) VALUES ('m', 's', 'user', 0, ?)",
        (_raw("sdk-cli"),),
    )
    raw.commit()
    raw.close()
    with Database(db_path) as db:
        assert db.conn is not None
        counts = [
            db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in ("source_files", "sessions", "messages")
        ]
    assert counts == [1, 1, 1]


# --------------------------------------------------------------------------- capture


def _line(uuid: str, entrypoint: str | None, *, kind: str = "user") -> dict[str, object]:
    record: dict[str, object] = {
        "type": kind,
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": "sess",
        "timestamp": "2026-04-29T00:00:00Z",
        "cwd": _PROJECT,
        "message": {"content": f"hello {uuid}"},
    }
    if entrypoint is not None:
        record["entrypoint"] = entrypoint
    return record


def _write(path: Path, records: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_session_entrypoint_prefers_first_non_null() -> None:
    raws: list[object] = [{"type": "summary"}, "junk", {"entrypoint": ""}, {"entrypoint": "sdk-ts"}]
    assert session_entrypoint([*raws, {"entrypoint": "cli"}]) == "sdk-ts"
    assert session_entrypoint([{"type": "summary"}]) is None


def test_file_entrypoint_skips_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('not json\n{"type": "x"}\n{"entrypoint": "sdk-py"}\n', encoding="utf-8")
    assert file_entrypoint(path) == "sdk-py"


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        ([_line("a", "cli"), _line("b", "cli")], "cli"),
        ([_line("a", None), _line("b", "sdk-cli"), _line("c", "cli")], "sdk-cli"),
        ([_line("a", None)], None),
    ],
    ids=["cli", "mixed", "unknown"],
)
def test_index_records_entrypoint(
    db: Database, tmp_path: Path, records: list[dict[str, object]], expected: str | None
) -> None:
    path = _write(tmp_path / "proj" / "sess.jsonl", records)
    assert index_file(db, path).error is None
    assert db.conn is not None
    assert db.conn.execute("SELECT entrypoint FROM sessions").fetchone()[0] == expected


def test_reindex_keeps_entrypoint(db: Database, tmp_path: Path) -> None:
    path = _write(tmp_path / "proj" / "sess.jsonl", [_line("a", "sdk-py")])
    index_file(db, path)
    index_file(db, path, force=True)
    assert db.conn is not None
    assert [tuple(r) for r in db.conn.execute("SELECT entrypoint FROM sessions")] == [("sdk-py",)]


def test_index_fills_entrypoint_on_a_session_another_file_created(
    db: Database, tmp_path: Path
) -> None:
    """A resumed transcript shares the session row; its entrypoint fills an unknown one."""
    first = _write(tmp_path / "a" / "sess.jsonl", [_line("a", None)])
    second = _write(tmp_path / "b" / "sess.jsonl", [_line("b", "sdk-cli")])
    index_file(db, first)
    index_file(db, second)
    assert db.conn is not None
    assert [tuple(r) for r in db.conn.execute("SELECT entrypoint FROM sessions")] == [("sdk-cli",)]


# --------------------------------------------------------------------------- filtering


def _stats(db: Path, family: str, capsys: pytest.CaptureFixture[str], *extra: str) -> object:
    return _json(db, ["stats", family, "--json", *extra], capsys)["stats"]


def test_stats_sessions_default_counts_interactive_only(
    mixed_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _stats(mixed_db, "sessions", capsys)
    assert isinstance(body, dict)
    assert body["total"] == len(_INTERACTIVE)
    assert body["include_sdk"] is False
    body = _stats(mixed_db, "sessions", capsys, "--include-sdk")
    assert isinstance(body, dict)
    assert body["total"] == len(_SESSIONS)
    assert body["include_sdk"] is True


@pytest.mark.parametrize(
    ("family", "key", "default", "everything"),
    [
        ("tools", "total", 2, 5),
        ("commands", "total", 2, 5),
        ("model", "total", 2, 5),
        ("files", "total", 2, 5),
        ("hooks", "total", 2, 5),
    ],
)
def test_stats_families_leave_sdk_out(
    mixed_db: Path,
    capsys: pytest.CaptureFixture[str],
    family: str,
    key: str,
    default: int,
    everything: int,
) -> None:
    body = _stats(mixed_db, family, capsys)
    assert isinstance(body, dict)
    assert body[key] == default
    body = _stats(mixed_db, family, capsys, "--include-sdk")
    assert isinstance(body, dict)
    assert body[key] == everything


def test_stats_files_with_project_leaves_sdk_out(
    mixed_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _stats(mixed_db, "files", capsys, "--project", _PROJECT)
    assert isinstance(body, dict)
    assert body["total"] == len(_INTERACTIVE)


def test_stats_model_names_only_interactive_models(
    mixed_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _stats(mixed_db, "model", capsys)
    assert isinstance(body, dict)
    models = {row["model"] for row in body["by_model"]}
    assert models == {f"model-{sid}" for sid in _INTERACTIVE}


def test_summary_default_and_include_sdk(
    mixed_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _json(mixed_db, ["summary", "--json"], capsys)["summary"]
    assert isinstance(body, dict)
    assert body["include_sdk"] is False
    assert body["sessions"]["total"] == len(_INTERACTIVE)
    assert body["tools"]["total"] == len(_INTERACTIVE)
    body = _json(mixed_db, ["summary", "--json", "--include-sdk"], capsys)["summary"]
    assert isinstance(body, dict)
    assert body["sessions"]["total"] == len(_SESSIONS)
    assert body["files"]["total"] == len(_SESSIONS)


def test_summary_prose_names_the_sdk_scope(
    mixed_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--db", str(mixed_db), "summary"]) == 0
    assert "sdk=excluded" in capsys.readouterr().out


def test_diff_default_and_include_sdk(mixed_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    body = _json(mixed_db, ["diff", "--json"], capsys)["diff"]
    assert isinstance(body, dict)
    current = body["current"]
    assert body["include_sdk"] is False
    assert current["sessions_count"] == len(_INTERACTIVE)
    assert current["tool_calls_total"] == len(_INTERACTIVE)
    assert current["commands_total"] == len(_INTERACTIVE)
    assert current["files_count"] == len(_INTERACTIVE)
    assert set(current["model_histogram"]) == {f"model-{sid}" for sid in _INTERACTIVE}
    body = _json(mixed_db, ["diff", "--json", "--include-sdk"], capsys)["diff"]
    assert isinstance(body, dict)
    assert body["current"]["sessions_count"] == len(_SESSIONS)
    assert body["current"]["tool_calls_total"] == len(_SESSIONS)


def _hit_sessions(body: object) -> set[str]:
    assert isinstance(body, dict)
    return {hit["session_id"] for hit in body["hits"]}


def test_search_default_and_include_sdk(mixed_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    body = _json(mixed_db, ["search", "needle", "--json", "--limit", "100"], capsys)["search"]
    assert _hit_sessions(body) == _INTERACTIVE
    assert isinstance(body, dict)
    assert body["filters"]["include_sdk"] is False
    # 2 messages + 1 tool result per session.
    assert body["total"] == 3 * len(_INTERACTIVE)
    argv = ["search", "needle", "--json", "--limit", "100", "--include-sdk"]
    body = _json(mixed_db, argv, capsys)["search"]
    assert _hit_sessions(body) == set(_SESSIONS)
    assert isinstance(body, dict)
    assert body["total"] == 3 * len(_SESSIONS)


def test_search_session_flag_reaches_an_sdk_session(
    mixed_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["search", "needle", "--json", "--session", "s-sdk-cli"]
    body = _json(mixed_db, argv, capsys)["search"]
    assert _hit_sessions(body) == {"s-sdk-cli"}


def test_search_tool_filter_leaves_sdk_out(
    mixed_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["search", "needle", "--json", "--tool", "Bash", "--limit", "100"]
    body = _json(mixed_db, argv, capsys)["search"]
    assert _hit_sessions(body) == _INTERACTIVE


def test_sessions_lists_entrypoint_and_filters(
    mixed_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _json(mixed_db, ["sessions", "--json"], capsys)["sessions"]
    assert isinstance(body, dict)
    assert body["include_sdk"] is False
    assert {item["id"]: item["entrypoint"] for item in body["items"]} == {
        sid: _SESSIONS[sid] for sid in _INTERACTIVE
    }
    body = _json(mixed_db, ["sessions", "--json", "--include-sdk"], capsys)["sessions"]
    assert isinstance(body, dict)
    assert {item["id"]: item["entrypoint"] for item in body["items"]} == _SESSIONS


def test_sessions_prose_has_entry_column(
    mixed_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--db", str(mixed_db), "sessions", "--include-sdk"]) == 0
    out = capsys.readouterr().out
    assert "entry" in out
    assert "sdk-inferred" in out


@pytest.mark.parametrize("sid", list(_SESSIONS))
def test_inspect_exposes_entrypoint(
    mixed_db: Path, capsys: pytest.CaptureFixture[str], sid: str
) -> None:
    body = _json(mixed_db, ["inspect", sid, "--json"], capsys)["inspect"]
    assert isinstance(body, dict)
    assert body["session"]["entrypoint"] == _SESSIONS[sid]
    timeline = _json(mixed_db, ["inspect", sid, "--json", "--timeline"], capsys)["inspect"]
    assert isinstance(timeline, dict)
    assert timeline["session"]["entrypoint"] == _SESSIONS[sid]


def test_inspect_prose_prints_entrypoint(
    mixed_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--db", str(mixed_db), "inspect", "s-sdk-py"]) == 0
    assert "entry     sdk-py" in capsys.readouterr().out


# --------------------------------------------------------------------------- old schema


@pytest.fixture
def v6_db(db_path: Path) -> Path:
    raw = build_db_at_version(db_path, 6)
    for n, sid in enumerate(("a", "b"), start=1):
        raw.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (?, ?, 1, 1, ?)",
            (n, f"/x/{sid}.jsonl", _RECENT),
        )
        raw.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at) "
            "VALUES (?, ?, ?, ?)",
            (sid, n, _PROJECT, _RECENT),
        )
        raw.execute(
            "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
            "VALUES (?, ?, 'user', 0, ?, 'needle here', ?)",
            (f"{sid}-m", sid, _RECENT, _raw("sdk-cli")),
        )
    raw.commit()
    raw.close()
    return db_path


@pytest.mark.parametrize(
    "argv",
    [
        ["stats", "sessions", "--json"],
        ["summary", "--json"],
        ["diff", "--json"],
        ["search", "needle", "--json"],
    ],
    ids=lambda a: a[0],
)
def test_pre_entrypoint_db_reads_everything_and_says_so(
    v6_db: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert main(["--db", str(v6_db), *argv]) == 0
    captured = capsys.readouterr()
    assert "predates session entrypoints" in captured.err
    json.loads(captured.out)
    with Database(v6_db, migrate=False) as db:
        assert db.schema_version() == 6


def test_pre_entrypoint_db_counts_sdk_sessions(
    v6_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _stats(v6_db, "sessions", capsys)
    assert isinstance(body, dict)
    assert body["total"] == 2
    body = _json(v6_db, ["sessions", "--json"], capsys)["sessions"]
    assert isinstance(body, dict)
    assert [item["entrypoint"] for item in body["items"]] == [None, None]
    inspect = _json(v6_db, ["inspect", "a", "--json"], capsys)["inspect"]
    assert isinstance(inspect, dict)
    assert inspect["session"]["entrypoint"] is None


def test_include_sdk_is_quiet_on_pre_entrypoint_db(
    v6_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--db", str(v6_db), "stats", "sessions", "--include-sdk"]) == 0
    assert "predates" not in capsys.readouterr().err
