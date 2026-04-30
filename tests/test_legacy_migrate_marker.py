"""Tests for the deferred-table marker file IO and staleness detection."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from convo.legacy_migrate import (
    _DEFAULT_MARKER_PATH,
    _ERR_MARKER_STALE,
    _RESUME_DEFERRED_MSG,
    DeferredTable,
    _is_marker_stale,
    _marker_path,
    _read_marker,
    _write_marker,
    report_deferred_tables,
    run,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _ns(**kw: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "src": None,
        "dest": None,
        "dry_run": False,
        "no_keep_legacy": False,
        "json": False,
        "seed": 0xC0FFEE,
        "resume_deferred": True,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_marker_path_honors_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "m.json"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(target))
    assert _marker_path() == target


def test_marker_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVO_LEGACY_MARKER", raising=False)
    assert _marker_path() == _DEFAULT_MARKER_PATH


def test_marker_write_read_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "marker.json"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(target))

    src = tmp_path / "legacy.db"
    src.write_bytes(b"x" * 128)
    deferred: list[DeferredTable] = [
        DeferredTable(
            name="hook_tool_events",
            row_count=7,
            blocked_by="0002_live_hooks.sql",
        ),
    ]
    written = _write_marker(src, deferred)
    assert written == target
    marker = _read_marker(target)
    assert marker["schema_version"] == 1
    assert marker["source_path"] == str(src)
    assert marker["source_size"] == 128
    assert marker["deferred_tables"] == deferred
    datetime.fromisoformat(marker["migrated_at"])


def test_marker_read_rejects_unknown_schema(tmp_path: Path) -> None:
    target = tmp_path / "marker.json"
    target.write_text(json.dumps({"schema_version": 99}))
    with pytest.raises(ValueError, match="schema_version"):
        _read_marker(target)


def test_is_marker_stale_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "marker.json"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(target))
    src = tmp_path / "legacy.db"
    src.write_bytes(b"hello")
    _write_marker(src, [])
    marker = _read_marker(target)
    stale, reason = _is_marker_stale(marker, src)
    assert stale is False
    assert reason == ""


def test_is_marker_stale_size_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "marker.json"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(target))
    src = tmp_path / "legacy.db"
    src.write_bytes(b"hello")
    _write_marker(src, [])
    marker = _read_marker(target)
    src.write_bytes(b"hello world!")
    stale, reason = _is_marker_stale(marker, src)
    assert stale is True
    assert "size" in reason or "mtime" in reason


def test_is_marker_stale_missing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "marker.json"
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(target))
    src = tmp_path / "legacy.db"
    src.write_bytes(b"x")
    _write_marker(src, [])
    marker = _read_marker(target)
    src.unlink()
    stale, reason = _is_marker_stale(marker, src)
    assert stale is True
    assert "no longer exists" in reason


def test_report_deferred_tables_skips_missing(
    legacy_conn: sqlite3.Connection,
) -> None:
    out = report_deferred_tables(legacy_conn)
    assert out == []


def test_report_deferred_tables_picks_up_present(
    legacy_conn: sqlite3.Connection,
) -> None:
    legacy_conn.executescript(
        "CREATE TABLE hook_tool_events (id INTEGER);"
        "INSERT INTO hook_tool_events VALUES (1);"
        "INSERT INTO hook_tool_events VALUES (2);"
        "INSERT INTO hook_tool_events VALUES (3);",
    )
    out = report_deferred_tables(legacy_conn)
    assert len(out) == 1
    assert out[0]["name"] == "hook_tool_events"
    assert out[0]["row_count"] == 3
    assert out[0]["blocked_by"] == "0002_live_hooks.sql"


def test_resume_deferred_with_clean_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = tmp_path / "legacy.db"
    src.write_bytes(b"x")
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))
    _write_marker(src, [])

    args = _ns(resume_deferred=True)
    rc = run(args)  # type: ignore[arg-type]
    assert rc == 0
    assert _RESUME_DEFERRED_MSG in capsys.readouterr().out


def test_resume_deferred_with_stale_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src = tmp_path / "legacy.db"
    src.write_bytes(b"x")
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "marker.json"))
    _write_marker(src, [])
    src.unlink()

    args = _ns(resume_deferred=True)
    rc = run(args)  # type: ignore[arg-type]
    assert rc == 1
    err = capsys.readouterr().err
    assert "marker is stale" in err
    assert _ERR_MARKER_STALE.split("(")[0].strip() in err


def test_resume_deferred_no_marker_passes_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CONVO_LEGACY_MARKER", str(tmp_path / "missing.json"))
    args = _ns(resume_deferred=True)
    rc = run(args)  # type: ignore[arg-type]
    assert rc == 0
    assert _RESUME_DEFERRED_MSG in capsys.readouterr().out
