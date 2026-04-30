"""End-to-end orchestration tests for legacy_migrate.run()."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from convo import legacy_migrate as _lm
from convo.db import Database
from convo.legacy_migrate import (
    LegacySourceError,
    run,
    validate_legacy_source,
)
from tests.fixtures.legacy_minimal_seed import seed_legacy

if TYPE_CHECKING:
    from pathlib import Path


def _legacy_path(tmp: Path, name: str = "legacy.db") -> Path:
    p = tmp / name
    conn = sqlite3.connect(p)
    seed_legacy(conn)
    conn.close()
    return p


def _ns(**kw: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "src": None,
        "dest": None,
        "dry_run": False,
        "no_keep_legacy": False,
        "json": False,
        "seed": 0xC0FFEE,
        "resume_deferred": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_validate_legacy_source_accepts_legacy(tmp_path: Path) -> None:
    src = _legacy_path(tmp_path)
    validate_legacy_source(src)  # no exception


def test_validate_legacy_source_rejects_new_schema(tmp_path: Path) -> None:
    new = tmp_path / "v1.db"
    Database(new).open().close()
    with pytest.raises(LegacySourceError):
        validate_legacy_source(new)


def test_e2e_migration_lands_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _legacy_path(tmp_path)
    dest = tmp_path / "new.db"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))

    rc = run(_ns(src=src, dest=dest))  # type: ignore[arg-type]
    assert rc == 0
    assert dest.exists()

    with Database(dest) as db:
        assert db.conn is not None
        sf_count = db.conn.execute("SELECT count(*) FROM source_files").fetchone()[0]
        sess_count = db.conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        msg_count = db.conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        tc_count = db.conn.execute("SELECT count(*) FROM tool_calls").fetchone()[0]
        assert sf_count == 5
        assert sess_count == 2  # conv-A, conv-B (conv-C orphan dropped)
        # 5 valid roles (msg 6 with bad role dropped)
        assert msg_count == 5
        assert tc_count >= 1  # tc1 + tc2 (resolvable); tc3 dropped

        synth = db.conn.execute(
            "SELECT raw_json FROM messages LIMIT 1",
        ).fetchone()[0]
        decoded = json.loads(synth)
        assert decoded["_synthesized"] is True


def test_dry_run_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = _legacy_path(tmp_path)
    dest = tmp_path / "new.db"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))

    rc = run(_ns(src=src, dest=dest, dry_run=True))  # type: ignore[arg-type]
    assert rc == 0
    assert not dest.exists()
    assert not (tmp_path / "marker.json").exists()
    out = capsys.readouterr().out
    assert "migrating..." in out
    assert "source_files" in out


def test_marker_written_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _legacy_path(tmp_path)
    dest = tmp_path / "new.db"
    marker = tmp_path / "marker.json"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(marker))

    rc = run(_ns(src=src, dest=dest))  # type: ignore[arg-type]
    assert rc == 0
    assert marker.exists()
    payload = json.loads(marker.read_text())
    assert payload["schema_version"] == 1
    assert payload["source_path"] == str(src.resolve())


def test_marker_write_failure_is_non_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = _legacy_path(tmp_path)
    dest = tmp_path / "new.db"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))

    def _boom(_src: Path, _deferred: object) -> Path:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(_lm, "_write_marker", _boom)

    rc = run(_ns(src=src, dest=dest, json=True))  # type: ignore[arg-type]
    assert rc == 0
    err = capsys.readouterr().err
    assert "marker write failed" in err
    out = capsys.readouterr().out
    # JSON output also flagged
    if out:
        payload = json.loads(out)
        assert payload["deferred"]["marker_write_failed"] is True


def test_fresh_dest_failure_removes_dest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _legacy_path(tmp_path)
    dest = tmp_path / "new.db"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))

    def _boom(*_args: object, **_kw: object) -> object:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(_lm, "validate", _boom)

    rc = run(_ns(src=src, dest=dest))  # type: ignore[arg-type]
    assert rc == 2
    assert not dest.exists()


def test_json_success_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = _legacy_path(tmp_path)
    dest = tmp_path / "new.db"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))

    rc = run(_ns(src=src, dest=dest, json=True))  # type: ignore[arg-type]
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"
    assert payload["src"] == str(src.resolve())
    assert payload["dest"] == str(dest.resolve())
    assert payload["auto_renamed_legacy"] is False
    assert isinstance(payload["duration_ms"], int)
    assert {"table", "legacy_count", "new_count", "dropped"} <= set(
        payload["migrated"][0].keys(),
    )
    assert {"counts", "samples", "fts_probes"} <= set(
        payload["validation"].keys(),
    )
    assert {"marker_path", "marker_write_failed", "tables"} <= set(
        payload["deferred"].keys(),
    )


def test_json_failure_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = _legacy_path(tmp_path)
    dest = tmp_path / "new.db"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))

    def _boom(*_a: object, **_kw: object) -> object:
        msg = "induced failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(_lm, "validate", _boom)

    rc = run(_ns(src=src, dest=dest, json=True))  # type: ignore[arg-type]
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "data_error"
    assert "induced failure" in payload["error"]
    assert payload["dest_removed"] is True


def test_prose_success_output_wording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = _legacy_path(tmp_path)
    dest = tmp_path / "new.db"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))

    rc = run(_ns(src=src, dest=dest))  # type: ignore[arg-type]
    assert rc == 0
    out = capsys.readouterr().out
    assert "migrating..." in out
    assert "validation:" in out
    assert "migration complete in" in out


def test_seed_reproducibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = _legacy_path(tmp_path)
    dest1 = tmp_path / "new1.db"
    dest2 = tmp_path / "new2.db"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))

    rc = run(_ns(src=src, dest=dest1, json=True, seed=42))  # type: ignore[arg-type]
    out1 = capsys.readouterr().out

    rc = run(_ns(src=src, dest=dest2, json=True, seed=42))  # type: ignore[arg-type]
    out2 = capsys.readouterr().out

    p1 = json.loads(out1)
    p2 = json.loads(out2)
    # Validation samples and fts_probes counts identical for same seed
    assert p1["validation"]["samples"] == p2["validation"]["samples"]
    assert p1["validation"]["fts_probes"] == p2["validation"]["fts_probes"]
    assert rc == 0


def test_idempotent_rerun_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = _legacy_path(tmp_path)
    dest = tmp_path / "new.db"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))

    rc = run(_ns(src=src, dest=dest))  # type: ignore[arg-type]
    assert rc == 0

    capsys.readouterr()
    rc = run(_ns(src=src, dest=dest))  # type: ignore[arg-type]
    assert rc == 1
    err = capsys.readouterr().err
    assert "not empty" in err
