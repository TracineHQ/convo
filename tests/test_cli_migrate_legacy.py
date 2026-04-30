"""T3 smoke tests for `convo migrate-legacy` via subprocess."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from typing import TYPE_CHECKING

from convo.db import Database
from tests.fixtures.legacy_minimal_seed import seed_legacy

if TYPE_CHECKING:
    from pathlib import Path


def _make_legacy(path: Path) -> None:
    conn = sqlite3.connect(path)
    seed_legacy(conn)
    conn.close()


def _run(argv: list[str], env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **env_overrides}
    return subprocess.run(  # noqa: S603 — sys.executable + literal args, no shell
        [sys.executable, "-m", "convo", *argv],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_subprocess_happy_path_json(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.db"
    dest = tmp_path / "new.db"
    _make_legacy(legacy)

    result = _run(
        [
            "migrate-legacy",
            "--src",
            str(legacy),
            "--dest",
            str(dest),
            "--json",
            "--seed",
            "42",
        ],
        {
            "CONVO_LEGACY_MARKER": str(tmp_path / "marker.json"),
            "CONVO_BACKUP_DIR": str(tmp_path / "backups"),
        },
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["src"] == str(legacy)
    assert payload["dest"] == str(dest)
    assert isinstance(payload["duration_ms"], int)
    assert {"counts", "samples", "fts_probes"} <= set(payload["validation"].keys())
    # Dest opens as a v1 DB
    with Database(dest) as db:
        assert db.conn is not None
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_subprocess_dry_run_no_writes(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.db"
    dest = tmp_path / "new.db"
    marker = tmp_path / "marker.json"
    _make_legacy(legacy)

    result = _run(
        [
            "migrate-legacy",
            "--src",
            str(legacy),
            "--dest",
            str(dest),
            "--json",
            "--dry-run",
        ],
        {"CONVO_LEGACY_MARKER": str(marker)},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert not dest.exists()
    assert not marker.exists()


def test_subprocess_no_keep_legacy_refuses(tmp_path: Path) -> None:
    legacy = tmp_path / "convo.db"
    _make_legacy(legacy)

    # Same src and dest -> --no-keep-legacy refuses
    result = _run(
        [
            "migrate-legacy",
            "--src",
            str(legacy),
            "--dest",
            str(legacy),
            "--no-keep-legacy",
        ],
        {"CONVO_LEGACY_MARKER": str(tmp_path / "marker.json")},
    )
    assert result.returncode == 1
    assert "same path" in result.stderr


def test_subprocess_resume_deferred_stale_marker(tmp_path: Path) -> None:
    marker = tmp_path / "marker.json"
    # Point at a path that doesn't exist
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_path": str(tmp_path / "missing.db"),
                "source_size": 100,
                "source_mtime_ns": 0,
                "migrated_at": "2026-04-29T00:00:00Z",
                "deferred_tables": [],
            },
        ),
    )

    result = _run(
        ["migrate-legacy", "--resume-deferred"],
        {"CONVO_LEGACY_MARKER": str(marker)},
    )
    assert result.returncode == 1
    assert "stale" in result.stderr
