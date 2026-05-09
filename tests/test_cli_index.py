"""Tests for `convo index`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from convo.cli import _resolve_projects_dir, main
from convo.db import Database

if TYPE_CHECKING:
    from pathlib import Path


def _user_record(uuid: str, sid: str, text: str) -> dict[str, object]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": sid,
        "timestamp": "2026-04-29T00:00:00Z",
        "cwd": "/tmp/proj",
        "gitBranch": "main",
        "message": {"content": text},
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    sid_a = "11111111-1111-1111-1111-111111111111"
    sid_b = "22222222-2222-2222-2222-222222222222"
    _write_jsonl(root / "alpha" / f"{sid_a}.jsonl", [_user_record("u1", sid_a, "hi")])
    _write_jsonl(root / "beta" / f"{sid_b}.jsonl", [_user_record("u2", sid_b, "hello")])
    return root


@pytest.fixture
def live_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(db_path))
    return db_path


def test_index_populates_db(
    live_db: Path,
    projects_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["index", "--projects-dir", str(projects_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "indexed" in out
    assert "Indexed 2 files" in out

    with Database(live_db) as db:
        assert db.conn is not None
        assert db.conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 2
        assert db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


@pytest.mark.usefixtures("live_db")
def test_index_json_envelope(
    projects_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["index", "--projects-dir", str(projects_dir), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["schema_version"] == 1
    body = payload["index"]
    assert body["status"] == "success"
    assert body["files_seen"] == 2
    assert body["files_indexed"] == 2
    assert body["files_skipped"] == 0
    assert body["files_failed"] == 0
    assert body["rows_inserted"]["messages"] == 2
    assert body["unknown_record_types"] == {}
    assert body["errors"] == []
    assert isinstance(body["duration_ms"], int)


def test_index_dry_run_writes_nothing(
    live_db: Path,
    projects_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["index", "--projects-dir", str(projects_dir), "--dry-run"])
    assert rc == 0
    capsys.readouterr()

    with Database(live_db) as db:
        assert db.conn is not None
        assert db.conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.usefixtures("live_db")
def test_index_dry_run_json(
    projects_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["index", "--projects-dir", str(projects_dir), "--dry-run", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["index"]["status"] == "success"
    assert payload["index"]["files_indexed"] == 2


@pytest.mark.usefixtures("live_db")
def test_index_empty_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "empty-projects"
    empty.mkdir()
    rc = main(["index", "--projects-dir", str(empty), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["index"]["status"] == "success"
    assert payload["index"]["files_seen"] == 0
    assert payload["index"]["files_indexed"] == 0


@pytest.mark.usefixtures("live_db")
def test_index_partial_with_corrupt_file(
    projects_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad_sid = "99999999-9999-9999-9999-999999999999"
    bad = projects_dir / "broken" / f"{bad_sid}.jsonl"
    bad.parent.mkdir()
    bad.write_text(
        json.dumps(_user_record("u1", bad_sid, "hi")) + "\n" + '{"type":\n',
        encoding="utf-8",
    )

    rc = main(["index", "--projects-dir", str(projects_dir), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    body = payload["index"]
    assert body["status"] == "partial"
    assert body["files_indexed"] == 2
    assert body["files_failed"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["line"] == 2
    assert "Invalid JSON" in body["errors"][0]["message"]


@pytest.mark.usefixtures("live_db")
def test_index_full_reindexes(
    projects_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["index", "--projects-dir", str(projects_dir)])
    capsys.readouterr()

    rc = main(["index", "--projects-dir", str(projects_dir), "--full", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["index"]["files_indexed"] == 2
    assert payload["index"]["files_skipped"] == 0


@pytest.mark.usefixtures("live_db")
def test_index_missing_projects_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["index", "--projects-dir", str(tmp_path / "does-not-exist")])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("convo: ")
    assert "does not exist" in err


@pytest.mark.usefixtures("live_db")
def test_index_progress_lines_only_when_not_json(
    projects_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["index", "--projects-dir", str(projects_dir)])
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith(("1/", "2/"))]
    assert len(lines) == 2


def test_resolve_projects_dir_explicit(tmp_path: Path) -> None:
    assert _resolve_projects_dir(tmp_path) == tmp_path


def test_resolve_projects_dir_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert _resolve_projects_dir(None) == tmp_path


def test_resolve_projects_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PROJECTS_DIR", raising=False)
    assert _resolve_projects_dir(None).name == "projects"


def test_top_level_help_lists_index(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "index" in out
    assert "CLAUDE_PROJECTS_DIR" in out


def test_index_help_shows_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["index", "--help"])
    out = capsys.readouterr().out
    assert "--full" in out
    assert "--projects-dir" in out
    assert "--dry-run" in out
    assert "--json" in out
    assert "CLAUDE_PROJECTS_DIR" in out


@pytest.mark.usefixtures("live_db")
def test_index_unknown_records_in_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "projects"
    sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    path = root / "alpha" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    records = [_user_record("u1", sid, "hi"), {"type": "alien", "sessionId": sid}]
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n",
        encoding="utf-8",
    )
    rc = main(["index", "--projects-dir", str(root), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["index"]["unknown_record_types"] == {"alien": 1}


@pytest.mark.usefixtures("live_db")
def test_index_unknown_records_in_prose(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "projects"
    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    path = root / "alpha" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    records = [_user_record("u1", sid, "hi"), {"type": "alien", "sessionId": sid}]
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n",
        encoding="utf-8",
    )
    rc = main(["index", "--projects-dir", str(root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Unknown record types" in out
    assert "alien" in out


@pytest.mark.usefixtures("live_db")
def test_index_status_error_when_all_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "projects"
    sid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    bad = root / "alpha" / f"{sid}.jsonl"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"type":\n', encoding="utf-8")
    rc = main(["index", "--projects-dir", str(root), "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    body = payload["index"]
    assert body["status"] == "error"
    assert body["files_failed"] == 1
    assert body["files_indexed"] == 0


@pytest.mark.usefixtures("live_db")
def test_index_partial_on_idempotent_rerun_with_persistent_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A re-run where every healthy file is skipped-unchanged and one file still
    fails must report status=partial, not status=error.
    """
    root = tmp_path / "projects"
    good_sid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    bad_sid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    _write_jsonl(root / "alpha" / f"{good_sid}.jsonl", [_user_record("u1", good_sid, "hi")])
    bad = root / "broken" / f"{bad_sid}.jsonl"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"type":\n', encoding="utf-8")

    rc = main(["index", "--projects-dir", str(root), "--json"])
    assert rc == 0
    first = json.loads(capsys.readouterr().out)["index"]
    assert first["status"] == "partial"
    assert first["files_indexed"] == 1
    assert first["files_failed"] == 1

    rc = main(["index", "--projects-dir", str(root), "--json"])
    assert rc == 0
    second = json.loads(capsys.readouterr().out)["index"]
    assert second["status"] == "partial"
    assert second["files_indexed"] == 0
    assert second["files_failed"] == 1
    assert second["files_skipped"] >= 1


@pytest.mark.usefixtures("live_db")
def test_index_projects_dir_pointing_at_file_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "oops.txt"
    target.write_text("not a directory\n", encoding="utf-8")
    rc = main(["index", "--projects-dir", str(target)])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("convo: ")
    assert "not a directory" in err


@pytest.mark.usefixtures("live_db")
def test_index_guard_explicit_path_missing_returns_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "does-not-exist.jsonl"
    rc = main(["index-guard", "--path", str(missing)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "path not found" in err
    assert str(missing) in err


@pytest.mark.usefixtures("live_db")
def test_index_guard_explicit_path_missing_json_returns_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "does-not-exist.jsonl"
    rc = main(["index-guard", "--path", str(missing), "--json"])
    assert rc == 1
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert envelope["guard"]["status"] == "no_log"


@pytest.mark.usefixtures("live_db")
def test_index_guard_no_path_no_log_returns_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Auto-discovery with no log present is a clean exit-0 (the user didn't
    # ask for a specific file). Pinned so the explicit-miss → 1 change above
    # doesn't regress the no-arg case.
    monkeypatch.setenv("GUARD_DECISIONS_PATH", str(tmp_path / "nope.jsonl"))
    rc = main(["index-guard"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no guard JSONL log found" in err
