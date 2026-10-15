"""`convo migrate`, and read commands that no longer migrate on open."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from convo.cli import main
from convo.db import SCHEMA_VERSION
from tests._seed import build_db_at_version, seed_raw_session

_SID = "5e551011-0000-4000-8000-000000000042"


def _version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _db_at(path: Path, version: int) -> Path:
    raw = build_db_at_version(path, version)
    seed_raw_session(raw, sid=_SID)
    raw.close()
    return path


@pytest.fixture
def v4_db(tmp_path: Path) -> Path:
    return _db_at(tmp_path / "convo.db", 4)


@pytest.fixture
def v3_db(tmp_path: Path) -> Path:
    return _db_at(tmp_path / "convo.db", 3)


_READERS: list[list[str]] = [
    ["info", "--json"],
    ["search", "needle", "--json"],
    ["inspect", _SID[:8], "--json"],
    ["stats", "tools", "--json"],
    ["summary", "--json"],
    ["diff", "--json"],
]


@pytest.mark.parametrize("argv", _READERS, ids=lambda a: a[0])
def test_read_commands_do_not_migrate(
    v4_db: Path,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--db", str(v4_db), *argv])
    assert rc == 0, capsys.readouterr()
    payload = json.loads(capsys.readouterr().out)
    assert "error" not in payload
    assert _version(v4_db) == 4


@pytest.mark.parametrize("argv", _READERS, ids=lambda a: a[0])
def test_reader_on_too_old_schema_returns_migrate_envelope(
    v3_db: Path,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--db", str(v3_db), *argv])
    assert rc == 1
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["schema_version"] == 2
    assert "convo migrate" in payload["error"]["message"]
    assert out.err == ""
    assert _version(v3_db) == 3


def test_reader_on_too_old_schema_prose_says_run_migrate(
    v3_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--db", str(v3_db), "search", "needle"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "schema version 3" in err
    assert "convo migrate" in err


def test_search_format_json_errors_use_the_envelope(
    v3_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--db", str(v3_db), "search", "needle", "--format", "json"])
    assert rc == 1
    assert "convo migrate" in json.loads(capsys.readouterr().out)["error"]["message"]


def test_reader_creates_a_missing_db_at_current_schema(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "fresh" / "convo.db"
    rc = main(["--db", str(db_path), "info", "--json"])
    assert rc == 0
    info = json.loads(capsys.readouterr().out)["info"]
    assert info["db_schema_version"] == SCHEMA_VERSION
    assert info["pending_migrations"] == 0
    assert _version(db_path) == SCHEMA_VERSION


def test_info_reports_pending_migrations(v4_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--db", str(v4_db), "info", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)["info"]
    assert info["db_schema_version"] == 4
    assert info["pending_migrations"] == SCHEMA_VERSION - 4

    assert main(["--db", str(v4_db), "info"]) == 0
    assert "run: convo migrate" in capsys.readouterr().out


def test_backup_does_not_migrate(
    v4_db: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dest = tmp_path / "copy.db"
    assert main(["--db", str(v4_db), "backup", str(dest)]) == 0
    capsys.readouterr()
    assert _version(v4_db) == 4
    assert _version(dest) == 4


def test_migrate_applies_pending(v4_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--db", str(v4_db), "migrate", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    body = payload["migrate"]
    assert body["status"] == "migrated"
    assert body["from_version"] == 4
    assert body["to_version"] == SCHEMA_VERSION
    assert [m["version"] for m in body["applied"]] == list(range(5, SCHEMA_VERSION + 1))
    assert body["applied"][0]["filename"] == "0005_fk_child_indexes.sql"
    assert body["applied"][0]["duration_ms"] >= 0
    assert body["backup_path"] is None
    assert _version(v4_db) == SCHEMA_VERSION


def test_migrate_prose_names_each_migration(
    v4_db: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--db", str(v4_db), "migrate"]) == 0
    out = capsys.readouterr().out
    assert "0005_fk_child_indexes.sql" in out
    assert f"4 -> {SCHEMA_VERSION}" in out


def test_migrate_when_up_to_date_is_a_no_op(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = _db_at(tmp_path / "convo.db", SCHEMA_VERSION)
    rc = main(["--db", str(db_path), "migrate", "--backup", "--json"])
    assert rc == 0
    body = json.loads(capsys.readouterr().out)["migrate"]
    assert body["status"] == "up_to_date"
    assert body["applied"] == []
    assert body["backup_path"] is None
    assert not (tmp_path / "convo-backups").exists()


def test_migrate_backup_snapshots_before_applying(
    v4_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CONVO_BACKUP_DIR", raising=False)
    rc = main(["--db", str(v4_db), "migrate", "--backup", "--json"])
    assert rc == 0
    body = json.loads(capsys.readouterr().out)["migrate"]
    assert body["backup_path"] is not None
    snapshot = Path(body["backup_path"])
    # Beside the DB, in the snapshot dir `convo snapshots` / `restore --latest` use.
    assert snapshot.parent == tmp_path / "convo-backups"
    assert snapshot.exists()
    # The snapshot is the pre-migration DB, complete and consistent.
    assert _version(snapshot) == 4
    conn = sqlite3.connect(snapshot)
    try:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 4
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert _version(v4_db) == SCHEMA_VERSION


def test_migrate_refuses_a_db_from_the_future(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "convo.db"
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    assert main(["--db", str(db_path), "migrate", "--json"]) == 1
    assert "refusing to downgrade" in json.loads(capsys.readouterr().out)["error"]["message"]


def test_index_still_migrates(
    v4_db: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "projects"
    empty.mkdir()
    assert main(["--db", str(v4_db), "index", "--projects-dir", str(empty), "--json"]) == 0
    capsys.readouterr()
    assert _version(v4_db) == SCHEMA_VERSION
