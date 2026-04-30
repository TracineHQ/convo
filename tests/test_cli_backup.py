"""Tests for `convo backup` and `convo backup --auto`."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from convo.cli import main
from convo.db import Database
from tests._seed import seed_source_file

if TYPE_CHECKING:
    from pathlib import Path


_AUTO_NAME_RE = re.compile(r"convo-\d{8}-\d{6}-\d{6}\.db")


def _populate(path: Path) -> None:
    with Database(path) as db:
        seed_source_file(db, path="/seed/a.jsonl")


def test_backup_explicit_dest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "live.db"
    out = tmp_path / "out.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    assert main(["backup", str(out)]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"backed up to {out}\n"

    with Database(out) as restored:
        assert restored.conn is not None
        assert restored.conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_backup_auto_writes_timestamped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "live.db"
    snaps = tmp_path / "snaps"
    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snaps))
    _populate(live)

    assert main(["backup", "--auto"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("snapshot written: ")
    assert _AUTO_NAME_RE.search(out)
    assert any(_AUTO_NAME_RE.match(p.name) for p in snaps.iterdir())


def test_db_flag_overrides_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_db = tmp_path / "from-env.db"
    explicit_db = tmp_path / "explicit.db"
    out = tmp_path / "out.db"
    monkeypatch.setenv("CONVO_DB", str(env_db))
    _populate(explicit_db)

    assert main(["--db", str(explicit_db), "backup", str(out)]) == 0
    assert explicit_db.exists()
    assert not env_db.exists()


def test_backup_overwrite_reports_clean_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "live.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)
    out = tmp_path / "out.db"
    out.write_bytes(b"existing")

    rc = main(["backup", str(out)])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("convo: ")
    assert str(out) in err
    assert "Traceback" not in err


def test_backup_empty_db_reports_clean_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "live.db"
    out = tmp_path / "out.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    # Note: live does NOT get populated — convo bootstraps an empty DB.

    rc = main(["backup", str(out)])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("convo: ")
    assert "empty convo DB" in err
    assert str(live) in err
    assert "Traceback" not in err
    assert not out.exists()


def test_unknown_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["nope"])
    assert exc.value.code == 2


def test_subprocess_smoke(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    out = tmp_path / "out.db"
    _populate(live)
    env = {**os.environ, "CONVO_DB": str(live)}

    result = subprocess.run(  # noqa: S603 — sys.executable + literal args, no shell
        [sys.executable, "-m", "convo", "backup", str(out)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert result.stdout == f"backed up to {out}\n"
    assert out.exists()

    with Database(out) as restored:
        assert restored.conn is not None
        assert restored.conn.execute("PRAGMA user_version").fetchone()[0] == 1
