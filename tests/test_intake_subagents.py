"""Subagent transcripts are indexed as sessions linked to their parent session."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from convo.cli import main
from convo.db import Database
from convo.intake.orchestrator import discover_transcripts, index_tree
from convo.intake.subagents import (
    SUBAGENT_SESSION_LIKE,
    read_meta,
    subagent_parent_id,
    subagent_session_id,
)
from convo.read.inspect import inspect_session, resolve_latest_session, resolve_session_id
from convo.read.search import search
from tests.perf._synth import write_transcript

_PARENT = "aaaaaaaa-0000-4000-8000-000000000001"
_OTHER = "bbbbbbbb-0000-4000-8000-000000000002"
# Transcript file stems are `agent-<id>`; the session id is `<id>.<parent>`.
_AGENT = "agent-a1b2c3"
_WF_AGENT = "agent-d4e5f6"
_OTHER_AGENT = "agent-999999"
_SID = f"a1b2c3.{_PARENT}"
_WF_SID = f"d4e5f6.{_PARENT}"
_OTHER_SID = f"999999.{_OTHER}"
_META = {"agentType": "Explore", "description": "Trace the dispersed camping rules"}


def _tree(tmp_path: Path) -> Path:
    """A parent session with a direct subagent and a workflow subagent, plus another session."""
    root = tmp_path / "projects"
    slug = root / "slug"
    write_transcript(slug / f"{_PARENT}.jsonl", _PARENT, start=0, count=4)
    write_transcript(slug / f"{_OTHER}.jsonl", _OTHER, start=0, count=4)

    subagents = slug / _PARENT / "subagents"
    write_transcript(subagents / f"{_AGENT}.jsonl", _AGENT, start=100, count=4)
    (subagents / f"{_AGENT}.meta.json").write_text(json.dumps(_META), encoding="utf-8")

    workflow = subagents / "workflows" / "wf_12345678-abc"
    write_transcript(workflow / f"{_WF_AGENT}.jsonl", _WF_AGENT, start=200, count=4)
    (workflow / f"{_WF_AGENT}.meta.json").write_text(
        json.dumps({"agentType": "workflow-subagent", "description": "Build step"}),
        encoding="utf-8",
    )
    (workflow / "journal.jsonl").write_text('{"type": "launched"}\n', encoding="utf-8")

    other_sub = slug / _OTHER / "subagents"
    write_transcript(other_sub / f"{_OTHER_AGENT}.jsonl", _OTHER_AGENT, start=300, count=4)
    return root


def _query(db: Database, sql: str, *params: object) -> list[tuple[Any, ...]]:
    assert db.conn is not None
    return [tuple(r) for r in db.conn.execute(sql, params).fetchall()]


# --- path helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "session_id"),
    [
        (f"/p/slug/{_PARENT}/subagents/{_AGENT}.jsonl", _SID),
        (f"/p/slug/{_PARENT}/subagents/workflows/wf_1-a/{_WF_AGENT}.jsonl", _WF_SID),
    ],
)
def test_subagent_paths_name_their_parent(path: str, session_id: str) -> None:
    found = Path(path)
    assert subagent_parent_id(found) == _PARENT
    assert subagent_session_id(found) == session_id


@pytest.mark.parametrize(
    "path",
    [
        f"/p/slug/{_PARENT}.jsonl",
        f"/p/slug/{_PARENT}/subagents/journal.jsonl",
        f"/p/slug/{_PARENT}/other/{_AGENT}.jsonl",
        f"/p/slug/{_AGENT}.jsonl",
    ],
)
def test_other_paths_are_not_subagents(path: str) -> None:
    assert subagent_parent_id(Path(path)) is None
    assert subagent_session_id(Path(path)) is None


def test_read_meta_tolerates_missing_and_malformed_files(tmp_path: Path) -> None:
    transcript = tmp_path / "agent-x.jsonl"
    assert read_meta(transcript) is None
    meta = tmp_path / "agent-x.meta.json"
    for body in ("not json", "[1, 2]", '"text"'):
        meta.write_text(body, encoding="utf-8")
        assert read_meta(transcript) is None
    meta.write_bytes(b"\xff\xfe")
    assert read_meta(transcript) is None
    meta.write_text(json.dumps(_META), encoding="utf-8")
    assert read_meta(transcript) == _META


# --- discovery ---------------------------------------------------------------


def test_discover_finds_direct_and_workflow_subagents(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    found = {p.relative_to(root).as_posix() for p in discover_transcripts(root)}
    assert found == {
        f"slug/{_PARENT}.jsonl",
        f"slug/{_OTHER}.jsonl",
        f"slug/{_PARENT}/subagents/{_AGENT}.jsonl",
        f"slug/{_PARENT}/subagents/workflows/wf_12345678-abc/{_WF_AGENT}.jsonl",
        f"slug/{_OTHER}/subagents/{_OTHER_AGENT}.jsonl",
    }


def test_discover_orders_subagents_with_everything_else_newest_first(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    oldest = root / "slug" / f"{_PARENT}.jsonl"
    os.utime(oldest, (1_700_000_000, 1_700_000_000))
    middle = root / "slug" / _PARENT / "subagents" / f"{_AGENT}.jsonl"
    os.utime(middle, (1_700_000_100, 1_700_000_100))
    found = discover_transcripts(root)
    assert found.index(middle) < found.index(oldest)


def test_discover_session_scope_includes_that_sessions_subagents_only(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    found = {p.relative_to(root).as_posix() for p in discover_transcripts(root, session_id=_PARENT)}
    assert found == {
        f"slug/{_PARENT}.jsonl",
        f"slug/{_PARENT}/subagents/{_AGENT}.jsonl",
        f"slug/{_PARENT}/subagents/workflows/wf_12345678-abc/{_WF_AGENT}.jsonl",
    }


def test_discover_session_scope_finds_subagents_without_a_top_level_transcript(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)
    (root / "slug" / f"{_PARENT}.jsonl").unlink()
    found = discover_transcripts(root, session_id=_PARENT)
    assert len(found) == 2
    assert all(subagent_parent_id(p) == _PARENT for p in found)


# --- indexing ----------------------------------------------------------------


def test_subagents_become_sessions_linked_to_their_parent(db: Database, tmp_path: Path) -> None:
    report = index_tree(db, _tree(tmp_path))
    assert report.files_seen == 5
    assert report.files_failed == 0

    ids = {r[0] for r in _query(db, "SELECT id FROM sessions")}
    assert ids == {_PARENT, _OTHER, _SID, _WF_SID, _OTHER_SID}
    children = _query(
        db,
        "SELECT id FROM sessions WHERE id LIKE ? AND substr(id, instr(id, '.') + 1) = ?",
        SUBAGENT_SESSION_LIKE,
        _PARENT,
    )
    assert sorted(r[0] for r in children) == [_SID, _WF_SID]


def test_subagent_rows_carry_the_subagent_session_id(db: Database, tmp_path: Path) -> None:
    index_tree(db, _tree(tmp_path))
    # 4 synthetic records (user prompt, assistant tool_use, tool_result,
    # assistant text) plus the meta message.
    assert _query(db, "SELECT COUNT(*) FROM messages WHERE session_id = ?", _SID) == [(5,)]
    assert _query(db, "SELECT COUNT(*) FROM tool_calls WHERE session_id = ?", _SID) == [(1,)]
    (path,) = _query(
        db,
        "SELECT sf.path FROM source_files sf JOIN sessions s ON s.source_file_id = sf.id "
        "WHERE s.id = ?",
        _SID,
    )
    assert path[0].endswith(f"{_AGENT}.jsonl")


def test_meta_becomes_a_leading_searchable_system_message(db: Database, tmp_path: Path) -> None:
    index_tree(db, _tree(tmp_path))
    rows = _query(
        db,
        "SELECT role, seq, content, raw_json FROM messages WHERE session_id = ? "
        "ORDER BY seq LIMIT 2",
        _SID,
    )
    (role, seq, content, raw_json), (second_role, second_seq, _, _) = rows
    assert (role, seq) == ("system", 0)
    assert content == "Subagent (Explore): Trace the dispersed camping rules"
    assert json.loads(str(raw_json)) == {
        "type": "subagent_meta",
        "parent_session_id": _PARENT,
        **_META,
    }
    assert (second_role, second_seq) == ("user", 1)

    hits = list(search(db, "camping rules"))
    assert [(h.session_id, h.role, h.position) for h in hits] == [(_SID, "system", 1)]


def test_workflow_subagent_meta_is_kept_too(db: Database, tmp_path: Path) -> None:
    index_tree(db, _tree(tmp_path))
    rows = _query(
        db,
        "SELECT content FROM messages WHERE session_id = ? AND role = 'system'",
        _WF_SID,
    )
    assert rows == [("Subagent (workflow-subagent): Build step",)]


@pytest.mark.parametrize("meta", [None, "not json", "[]", "{}", '{"agentType": 7}'])
def test_a_missing_or_useless_meta_adds_no_message(
    db: Database,
    tmp_path: Path,
    meta: str | None,
) -> None:
    root = _tree(tmp_path)
    meta_path = root / "slug" / _PARENT / "subagents" / f"{_AGENT}.meta.json"
    if meta is None:
        meta_path.unlink()
    else:
        meta_path.write_text(meta, encoding="utf-8")

    report = index_tree(db, root)

    assert report.files_failed == 0
    assert _query(db, "SELECT COUNT(*) FROM messages WHERE session_id = ?", _SID) == [(4,)]
    assert _query(db, "SELECT MIN(seq) FROM messages WHERE session_id = ?", _SID) == [(0,)]


def test_top_level_sessions_get_no_meta_message(db: Database, tmp_path: Path) -> None:
    index_tree(db, _tree(tmp_path))
    assert _query(db, "SELECT COUNT(*) FROM messages WHERE session_id = ?", _PARENT) == [(4,)]
    # Only the two subagents that have a meta file.
    assert _query(db, "SELECT COUNT(*) FROM messages WHERE role = 'system'") == [(2,)]


def test_reindexing_leaves_unchanged_subagents_alone(db: Database, tmp_path: Path) -> None:
    root = _tree(tmp_path)
    index_tree(db, root)
    again = index_tree(db, root)
    assert again.files_indexed == 0
    assert again.files_skipped_unchanged == 5
    assert _query(db, "SELECT COUNT(*) FROM sessions") == [(5,)]


def test_a_grown_subagent_transcript_is_reindexed_in_place(db: Database, tmp_path: Path) -> None:
    root = _tree(tmp_path)
    index_tree(db, root)
    grown = root / "slug" / _PARENT / "subagents" / f"{_AGENT}.jsonl"
    write_transcript(grown, _AGENT, start=104, count=4)

    again = index_tree(db, root)

    assert again.files_indexed == 1
    assert _query(db, "SELECT COUNT(*) FROM messages WHERE session_id = ?", _SID) == [(9,)]
    assert _query(db, "SELECT COUNT(*) FROM messages WHERE role = 'system'") == [(2,)]


def test_session_id_prefixes_resolve_to_the_right_session(db: Database, tmp_path: Path) -> None:
    index_tree(db, _tree(tmp_path))
    # The parent comes last in a subagent's id, so the parent's prefix is still unique ...
    assert resolve_session_id(db, _PARENT[:8]) == _PARENT
    # ... and the agent id, which Claude Code reports as `agentId`, finds the subagent.
    assert resolve_session_id(db, "a1b2c3") == _SID


def test_inspect_shows_a_subagent_session(db: Database, tmp_path: Path) -> None:
    index_tree(db, _tree(tmp_path))
    view = inspect_session(db, _SID)
    assert view.total_messages == 5
    assert view.messages[0].role == "system"
    assert view.messages[0].content.startswith("Subagent (Explore)")


def test_latest_session_is_never_a_subagent(db: Database, tmp_path: Path) -> None:
    index_tree(db, _tree(tmp_path))
    # The subagents' synthetic timestamps are later than either parent's.
    newest = _query(db, "SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1")
    assert "." in newest[0][0]
    assert resolve_latest_session(db) in {_PARENT, _OTHER}


# --- CLI ---------------------------------------------------------------------


def _index_json(
    db_path: Path,
    root: Path,
    *extra: str,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    argv = ["--db", str(db_path), "index", "--projects-dir", str(root), "--json", *extra]
    assert main(argv) == 0
    body: dict[str, Any] = json.loads(capsys.readouterr().out)["index"]
    return body


def _indexed_ids(db_path: Path) -> set[str]:
    with Database(db_path, migrate=False) as db:
        return {r[0] for r in _query(db, "SELECT id FROM sessions")}


def test_index_session_includes_that_sessions_subagents(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "convo.db"
    body = _index_json(db_path, _tree(tmp_path), "--session", _PARENT, capsys=capsys)
    assert body["files_seen"] == 3
    assert body["files_indexed"] == 3
    assert _indexed_ids(db_path) == {_PARENT, _SID, _WF_SID}


def test_index_session_scope_leaves_other_sessions_subagents_out(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "convo.db"
    _index_json(db_path, _tree(tmp_path), "--session", _OTHER, capsys=capsys)
    assert _indexed_ids(db_path) == {_OTHER, _OTHER_SID}


def test_index_without_a_session_covers_every_subagent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "convo.db"
    body = _index_json(db_path, _tree(tmp_path), capsys=capsys)
    assert body["files_seen"] == 5
    assert body["files_failed"] == 0
    assert _indexed_ids(db_path) == {_PARENT, _OTHER, _SID, _WF_SID, _OTHER_SID}


def test_index_dry_run_counts_subagents_as_new(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = _index_json(tmp_path / "convo.db", _tree(tmp_path), "--dry-run", capsys=capsys)
    assert body["files_seen"] == 5
    assert body["files_indexed"] == 5
