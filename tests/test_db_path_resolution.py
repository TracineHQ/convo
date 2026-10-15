"""Tests for resolve_db_path precedence: explicit > $CONVO_DB > default."""

from __future__ import annotations

from pathlib import Path

import pytest

from convo.cli import main
from convo.db import DEFAULT_DB_PATH, Database, resolve_db_path
from convo.read._db_access import open_ro
from tests._seed import seed_message, seed_session, seed_source_file, seed_tool_call


# Absolute paths come from tmp_path: on Windows a rooted path like "/x.db" has
# no drive, so making it absolute adds the current one.
def test_explicit_path_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVO_DB", str(tmp_path / "env" / "wins.db"))
    explicit = tmp_path / "nonexistent" / "x.db"
    assert resolve_db_path(explicit) == explicit


def test_explicit_str_arg_coerced_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVO_DB", raising=False)
    result = resolve_db_path(str(tmp_path / "nonexistent" / "y.db"))
    assert isinstance(result, Path)
    assert result == tmp_path / "nonexistent" / "y.db"


def test_env_var_used_without_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVO_DB", str(tmp_path / "env" / "path.db"))
    assert resolve_db_path() == tmp_path / "env" / "path.db"


def test_default_when_neither_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVO_DB", raising=False)
    assert resolve_db_path() == DEFAULT_DB_PATH


def test_env_tilde_is_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVO_DB", "~/foo.db")
    result = resolve_db_path()
    assert "~" not in str(result)
    assert result.is_absolute()


def test_relative_explicit_path_is_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_db_path("sub/rel.db") == tmp_path / "sub" / "rel.db"


def test_relative_env_path_is_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONVO_DB", "rel.db")
    assert resolve_db_path() == tmp_path / "rel.db"


def test_open_ro_accepts_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with Database(tmp_path / "rel.db"):
        pass
    monkeypatch.chdir(tmp_path)
    conn = open_ro("rel.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1
    finally:
        conn.close()


def _seed_relative_db(path: Path) -> None:
    with Database(path) as db:
        sfid = seed_source_file(db)
        sid = seed_session(
            db, sfid, sid="5e551011-0000-4000-8000-0000000000aa", project_path="/work/rel"
        )
        mid = seed_message(
            db, sid, content="relative path needle", timestamp="2026-10-15T00:00:00Z"
        )
        seed_tool_call(db, mid, sid, input_json='{"command": "echo needle"}')


_READ_COMMANDS: list[list[str]] = [
    ["info"],
    ["search", "needle"],
    ["search", "needle", "--format", "json"],
    ["inspect", "--latest"],
    ["stats", "tools"],
    ["summary"],
    ["diff"],
    ["projects"],
    ["tools"],
    ["sessions"],
]


@pytest.mark.parametrize("argv", _READ_COMMANDS, ids=" ".join)
@pytest.mark.parametrize("via", ["flag", "env"])
def test_read_commands_work_with_relative_db(
    argv: list[str],
    via: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: a relative --db / CONVO_DB failed with 'relative path can't be
    expressed as a file URI' on every read command."""
    _seed_relative_db(tmp_path / "rel.db")
    monkeypatch.chdir(tmp_path)
    if via == "flag":
        monkeypatch.delenv("CONVO_DB", raising=False)
        rc = main(["--db", "rel.db", *argv])
    else:
        monkeypatch.setenv("CONVO_DB", "rel.db")
        rc = main(argv)
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "file URI" not in captured.err
