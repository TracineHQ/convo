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

if TYPE_CHECKING:
    from pathlib import Path


_AUTO_NAME_RE = re.compile(r"convo-\d{8}-\d{6}-\d{6}\.db")


def test_backup_explicit_dest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "live.db"
    out = tmp_path / "out.db"
    monkeypatch.setenv("CONVO_DB", str(live))

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

    assert main(["backup", "--auto"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("snapshot written: ")
    assert _AUTO_NAME_RE.search(out)
    assert any(_AUTO_NAME_RE.match(p.name) for p in snaps.iterdir())


def test_backup_auto_with_prune_keep_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "live.db"
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    # Pre-seed 5 snapshots with staggered mtimes (oldest first).
    for i in range(5):
        p = snaps / f"convo-2026010{i}-000000-000000.db"
        p.write_bytes(b"")
        os.utime(p, (1700000000.0 + i, 1700000000.0 + i))

    monkeypatch.setenv("CONVO_DB", str(live))
    monkeypatch.setenv("CONVO_BACKUP_DIR", str(snaps))

    assert main(["backup", "--auto", "--prune", "--keep", "3"]) == 0
    out = capsys.readouterr().out
    assert "snapshot written: " in out
    # 5 pre-seeded + 1 new = 6 total; keep 3 → delete 3.
    assert "pruned 3 old snapshot(s)\n" in out

    remaining = list(snaps.glob("convo-*.db"))
    assert len(remaining) == 3


def test_db_flag_overrides_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_db = tmp_path / "from-env.db"
    explicit_db = tmp_path / "explicit.db"
    out = tmp_path / "out.db"
    monkeypatch.setenv("CONVO_DB", str(env_db))

    assert main(["--db", str(explicit_db), "backup", str(out)]) == 0
    assert explicit_db.exists()
    assert not env_db.exists()


def test_backup_overwrite_propagates_file_exists_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONVO_DB", str(tmp_path / "live.db"))
    out = tmp_path / "out.db"
    out.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match=str(out)):
        main(["backup", str(out)])


def test_unknown_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["nope"])
    assert exc.value.code == 2


def test_subprocess_smoke(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    out = tmp_path / "out.db"
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
