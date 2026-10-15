"""Transcript discovery: newest first, scoped by session, walked once per run."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from convo import cli
from convo.cli import main
from convo.db import Database
from convo.intake import orchestrator
from convo.intake.orchestrator import IndexResult, discover_transcripts, index_tree

if TYPE_CHECKING:
    from pathlib import Path

_SIDS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
)


def _write(path: Path, sid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        "uuid": f"u-{sid}",
        "parentUuid": None,
        "sessionId": sid,
        "timestamp": "2026-09-01T00:00:00Z",
        "cwd": "/synthetic",
        "message": {"content": f"hello {sid}"},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Three transcripts whose path order is the reverse of their mtime order."""
    root = tmp_path / "projects"
    for i, (slug, sid) in enumerate(zip(("alpha", "beta", "gamma"), _SIDS, strict=True)):
        path = root / slug / f"{sid}.jsonl"
        _write(path, sid)
        # alpha is oldest, gamma newest.
        stamp = 1_700_000_000 + i * 60
        os.utime(path, (stamp, stamp))
    return root


def test_discover_orders_newest_mtime_first(root: Path) -> None:
    found = discover_transcripts(root)
    assert [p.parent.name for p in found] == ["gamma", "beta", "alpha"]


def test_discover_breaks_mtime_ties_by_path(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    for slug, sid in zip(("b", "a"), _SIDS, strict=False):
        path = root / slug / f"{sid}.jsonl"
        _write(path, sid)
        os.utime(path, (1_700_000_000, 1_700_000_000))
    assert [p.parent.name for p in discover_transcripts(root)] == ["a", "b"]


def test_discover_scoped_to_one_session(root: Path) -> None:
    found = discover_transcripts(root, session_id=_SIDS[1])
    assert found == [root / "beta" / f"{_SIDS[1]}.jsonl"]


def test_discover_scoped_to_unknown_session_is_empty(root: Path) -> None:
    assert discover_transcripts(root, session_id="99999999-9999-4999-8999-999999999999") == []


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "*", "abc?", "[x]", "a\\b"])
def test_discover_rejects_unsafe_session_id(root: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="session id"):
        discover_transcripts(root, session_id=bad)


def test_index_tree_processes_newest_first(db: Database, root: Path) -> None:
    seen: list[IndexResult] = []
    index_tree(db, root, on_file=seen.append)
    assert [r.path.parent.name for r in seen] == ["gamma", "beta", "alpha"]


def test_index_tree_accepts_explicit_file_list(db: Database, root: Path) -> None:
    only = [root / "alpha" / f"{_SIDS[0]}.jsonl"]
    report = index_tree(db, root, files=only)
    assert report.files_seen == 1
    assert report.files_indexed == 1


def test_index_cli_walks_the_tree_once(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str | None] = []
    real = orchestrator.discover_transcripts

    def counting(projects_dir: Path, *, session_id: str | None = None) -> list[Path]:
        calls.append(session_id)
        return real(projects_dir, session_id=session_id)

    monkeypatch.setattr(orchestrator, "discover_transcripts", counting)
    monkeypatch.setattr(cli, "discover_transcripts", counting)

    rc = main(["--db", str(tmp_path / "convo.db"), "index", "--projects-dir", str(root)])

    assert rc == 0
    assert calls == [None]
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln[:2] in {"1/", "2/", "3/"}]
    assert [ln.split(" ")[0] for ln in lines] == ["1/3", "2/3", "3/3"]
    assert lines[0].startswith("1/3 gamma/")


def test_index_cli_session_scope_indexes_only_that_file(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "convo.db"
    rc = main(
        [
            "--db",
            str(db_path),
            "index",
            "--projects-dir",
            str(root),
            "--session",
            _SIDS[2],
            "--json",
        ],
    )
    assert rc == 0
    body = json.loads(capsys.readouterr().out)["index"]
    assert body["files_seen"] == 1
    assert body["files_indexed"] == 1
    with Database(db_path) as db:
        assert db.conn is not None
        sessions = [r[0] for r in db.conn.execute("SELECT id FROM sessions")]
    assert sessions == [_SIDS[2]]


def test_index_cli_rejects_unsafe_session(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "--db",
            str(tmp_path / "convo.db"),
            "index",
            "--projects-dir",
            str(root),
            "--session",
            "../etc",
            "--json",
        ],
    )
    assert rc == 1
    assert "session id" in json.loads(capsys.readouterr().out)["error"]["message"]
