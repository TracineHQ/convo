"""`position`: the 1-indexed message number shared by search and inspect.

A search hit's ``position`` must be exactly the number ``inspect`` prints and
``--from-message/--to-message`` accept, so a hit can be quoted in full with
``inspect <sid> --from-message P --to-message P --max-chars 0``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from convo.cli import main
from convo.db import Database
from convo.intake.positions import assign_positions
from convo.read.inspect import _MSG_SELECT, MESSAGE_ORDER_BY
from convo.read.search import _POSITION_SQL
from tests._seed import build_db_at_version

_SID = "5e551011-0000-4000-8000-000000000001"
_NEEDLE = "zebracorn"

# Message row: id, role, seq, timestamp, content, and an optional tool-call input.
type _Msg = tuple[str, str, int, str | None, str, str | None]


def _insert(conn: sqlite3.Connection, msgs: list[_Msg]) -> None:
    """Insert one source file, the session and `msgs` raw, with no stored positions."""
    conn.execute(
        "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
        "VALUES (1, '/synthetic/position', 0, 0, '2026-09-01T00:00:00Z')",
    )
    conn.execute(
        "INSERT INTO sessions(id, source_file_id, project_path, started_at) "
        "VALUES (?, 1, '/work/position', '2026-09-01T10:00:00Z')",
        (_SID,),
    )
    for mid, role, seq, ts, content, tool_input in msgs:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, '{}')",
            (mid, _SID, role, seq, ts, content),
        )
        if tool_input is not None:
            conn.execute(
                "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
                "started_at) VALUES (?, ?, ?, 0, 'Bash', ?, ?)",
                (f"tc-{mid}", mid, _SID, tool_input, ts),
            )
            conn.execute(
                "INSERT INTO tool_results(tool_call_id, message_id, output_text) VALUES (?, ?, ?)",
                (f"tc-{mid}", mid, f"output {_NEEDLE}"),
            )


def _seed(path: Path, msgs: list[_Msg], *, stored: bool) -> None:
    """Insert `msgs` raw; with `stored`, also number them as intake does.

    Without stored positions, search falls back to counting at query time.
    Both paths must give the same numbers.
    """
    with Database(path) as db:
        assert db.conn is not None
        _insert(db.conn, msgs)
        if stored:
            assign_positions(db.conn, {_SID: (min(m[2] for m in msgs), 1)})
        db.conn.commit()


def _json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    rc = main([*argv, "--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    payload: dict[str, Any] = json.loads(out)
    return payload


def _hits(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = _json(["search", _NEEDLE, "--limit", "500"], capsys)["search"][
        "hits"
    ]
    return hits


def _assert_hits_round_trip(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """Every positioned hit resolves, via an inspect range of one, to its message."""
    hits = _hits(capsys)
    for hit in hits:
        pos = hit["position"]
        assert isinstance(pos, int)
        (msg,) = _json(
            ["inspect", _SID, "--from-message", str(pos), "--to-message", str(pos)], capsys
        )["inspect"]["messages"]
        assert msg["position"] == pos
        if hit["kind"] == "message":
            assert msg["id"] == hit["id"]
        else:
            # tool_call and tool_result hits share the call's id and point at
            # the message that made the call.
            assert hit["id"] in [tc["id"] for tc in msg["tool_calls"]]
    return hits


# A mixed session: roles, tool calls, empty content, a NULL timestamp, and a
# seq/timestamp tie (m-b and m-a) that only the id tiebreaker orders.
_MIXED: list[_Msg] = [
    ("m-0", "user", 0, "2026-09-01T10:00:00Z", f"first {_NEEDLE}", None),
    ("m-1", "assistant", 1, "2026-09-01T10:00:01Z", "", f'{{"cmd": "echo {_NEEDLE}"}}'),
    ("m-2", "user", 2, None, "", None),
    ("m-b", "assistant", 3, "2026-09-01T10:00:03Z", f"tie beta {_NEEDLE}", None),
    ("m-a", "assistant", 3, "2026-09-01T10:00:03Z", f"tie alpha {_NEEDLE}", None),
    ("m-5", "system", 4, "2026-09-01T10:00:04Z", f"system {_NEEDLE}", None),
]
# Every _NEEDLE hit in _MIXED, keyed (kind, id), and the position it carries.
_MIXED_HIT_POSITIONS = {
    ("message", "m-0"): 1,
    ("tool_call", "tc-m-1"): 2,
    ("tool_result", "tc-m-1"): 2,
    ("message", "m-a"): 4,
    ("message", "m-b"): 5,
    ("message", "m-5"): 6,
}


@pytest.fixture(params=[True, False], ids=["stored", "fallback"])
def mixed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _seed(live, _MIXED, stored=request.param)
    return live


@pytest.mark.usefixtures("mixed")
def test_inspect_json_positions_match_prose_numbers(capsys: pytest.CaptureFixture[str]) -> None:
    msgs = _json(["inspect", _SID, "--max-chars", "0"], capsys)["inspect"]["messages"]
    assert [m["position"] for m in msgs] == [1, 2, 3, 4, 5, 6]
    # Ties on (seq, timestamp) are ordered by id, deterministically.
    assert [m["id"] for m in msgs] == ["m-0", "m-1", "m-2", "m-a", "m-b", "m-5"]

    assert main(["inspect", _SID, "--max-chars", "0"]) == 0
    out = capsys.readouterr().out
    assert f"4. A: 2026-09-01T10:00:03Z  tie alpha {_NEEDLE}" in out
    assert f"5. A: 2026-09-01T10:00:03Z  tie beta {_NEEDLE}" in out

    ranged = _json(["inspect", _SID, "--from-message", "4", "--to-message", "5"], capsys)
    assert [(m["position"], m["id"]) for m in ranged["inspect"]["messages"]] == [
        (4, "m-a"),
        (5, "m-b"),
    ]


@pytest.mark.usefixtures("mixed")
def test_inspect_timeline_json_positions(capsys: pytest.CaptureFixture[str]) -> None:
    events = _json(["inspect", _SID, "--timeline"], capsys)["inspect"]["timeline"]["events"]
    assert [(ev["role"], ev["position"]) for ev in events] == [
        ("user", 1),
        ("assistant", 2),
        ("tool_call", 2),  # a tool call carries its parent message's position
        ("user", 3),
        ("assistant", 4),
        ("assistant", 5),
        ("system", 6),
    ]
    ranged = _json(
        ["inspect", _SID, "--timeline", "--from-message", "5", "--to-message", "6"], capsys
    )["inspect"]["timeline"]["events"]
    assert [ev["position"] for ev in ranged] == [5, 6]


@pytest.mark.usefixtures("mixed")
def test_search_hits_carry_position(capsys: pytest.CaptureFixture[str]) -> None:
    hits = _assert_hits_round_trip(capsys)
    assert {(h["kind"], h["id"]): h["position"] for h in hits} == _MIXED_HIT_POSITIONS


@pytest.mark.parametrize("version", [4, 5])
def test_search_counts_positions_on_a_schema_without_stored_ones(
    version: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reads still accept schema 4 and 5, which predate message_positions (0006).

    Search counts each position at query time there instead of failing on the
    missing table, so positions stay right, and the read leaves the DB unmigrated.
    """
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    raw = build_db_at_version(live, version)
    _insert(raw, _MIXED)
    raw.commit()
    raw.close()
    hits = _assert_hits_round_trip(capsys)
    assert {(h["kind"], h["id"]): h["position"] for h in hits} == _MIXED_HIT_POSITIONS
    with Database(live, migrate=False) as db:
        assert db.schema_version() == version


@pytest.mark.usefixtures("mixed")
def test_search_prose_shows_position(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["search", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "position: 4" in out
    assert main(["search", _NEEDLE, "--fields", "session,position"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"{_SID[:8]}\t4" in lines
    assert f"{_SID[:8]}\t" not in lines  # every hit, tool_result included, has a position


_ROLES = st.sampled_from(["user", "assistant", "system"])
# Few distinct values so seq and timestamp ties (and NULL timestamps) are common.
_TIMESTAMPS = st.sampled_from([None, "2026-09-01T10:00:00Z", "2026-09-01T10:00:01Z"])
_CONTENT = st.sampled_from(["", "plain words", f"has {_NEEDLE} inside"])
_TOOL = st.sampled_from([None, '{"cmd": "ls"}', f'{{"cmd": "grep {_NEEDLE}"}}'])


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    rows=st.lists(
        st.tuples(_ROLES, st.integers(0, 3), _TIMESTAMPS, _CONTENT, _TOOL),
        min_size=1,
        max_size=12,
    )
)
def test_every_search_hit_round_trips_through_inspect(
    rows: list[tuple[str, int, str | None, str, str | None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    msgs: list[_Msg] = [
        (f"m{i:02d}", role, seq, ts, content, tool)
        for i, (role, seq, ts, content, tool) in enumerate(rows)
    ]
    for stored in (True, False):
        with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
            live = Path(tmp) / "convo.db"
            mp.setenv("CONVO_DB", str(live))
            _seed(live, msgs, stored=stored)
            _assert_hits_round_trip(capsys)


def test_inspect_select_uses_the_shared_message_order() -> None:
    """Search ranks positions by MESSAGE_ORDER_BY; inspect must list messages the same way."""
    assert _MSG_SELECT.endswith(" ORDER BY " + MESSAGE_ORDER_BY)
    # _POSITION_SQL hand-encodes this ordering; change both together.
    assert MESSAGE_ORDER_BY == "seq, timestamp, id"


# One EXPLAIN QUERY PLAN line per table read: "SEARCH b USING ..." on SQLite
# 3.36+, "SEARCH TABLE messages AS b USING ..." before that.
_PLAN_READ = re.compile(r"^(SCAN|SEARCH) (?:TABLE )?(\w+)(?: AS (\w+))?")


def test_position_reads_only_the_hit_rows(mixed: Path) -> None:
    """Position is a key lookup per hit (a range count as fallback), never a session scan.

    Regression guard for the 2.1.0 ROW_NUMBER() form, which read every message of
    each hit's session (~0.1 s per search with a hit in a 32k-message session).
    """
    conn = sqlite3.connect(mixed)
    try:
        # _POSITION_SQL is a module constant; nothing user-supplied is interpolated.
        sql = (
            f"EXPLAIN QUERY PLAN SELECT {_POSITION_SQL} FROM messages hm "  # noqa: S608
            "LEFT JOIN message_positions p ON p.message_id = hm.id WHERE hm.id = 'm-a'"
        )
        plan = [str(row[3]) for row in conn.execute(sql)]
    finally:
        conn.close()
    # (operation, table alias) per table read. The plan text varies by SQLite
    # version ("SEARCH p USING ..." vs "SEARCH TABLE message_positions AS p
    # USING ..."), so match the shape, not the exact line.
    reads = [(m[1], m[3] or m[2]) for line in plan if (m := _PLAN_READ.match(line))]
    # The hit's row, its stored position and the fallback counts are all read
    # by key or index range: no table is scanned.
    assert {"hm", "p", "b"} <= {alias for _, alias in reads}, plan
    assert all(op == "SEARCH" for op, _ in reads), plan
    # The fallback count seeks the rows before the hit (a seq range), not the
    # whole session.
    assert any("idx_messages_session" in line and "seq<?" in line for line in plan), plan


def test_search_reads_the_stored_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _seed(live, _MIXED, stored=True)
    conn = sqlite3.connect(live)
    conn.execute("UPDATE message_positions SET position = 99 WHERE message_id = 'm-a'")
    conn.commit()
    conn.close()
    hits = {(h["kind"], h["id"]): h["position"] for h in _hits(capsys)}
    assert hits[("message", "m-a")] == 99
    assert hits[("message", "m-b")] == 5


def test_search_positions_are_numbered_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hits from two sessions are numbered within their own session, never across both."""
    live = tmp_path / "two.bin"
    monkeypatch.setenv("CONVO_DB", str(live))
    other = "5e551011-0000-4000-8000-000000000002"
    with Database(live) as db:
        assert db.conn is not None
        for n, sid in enumerate((_SID, other), start=1):
            db.conn.execute(
                "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
                "VALUES (?, ?, 0, 0, '2026-09-01T00:00:00Z')",
                (n, f"/synthetic/{n}"),
            )
            db.conn.execute(
                "INSERT INTO sessions(id, source_file_id, project_path, started_at) "
                "VALUES (?, ?, '/work/two', '2026-09-01T10:00:00Z')",
                (sid, n),
            )
            for seq in range(3):
                db.conn.execute(
                    "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
                    "VALUES (?, ?, 'user', ?, ?, ?, '{}')",
                    (
                        f"{sid[-1]}-{seq}",
                        sid,
                        seq,
                        f"2026-09-01T10:0{n}:0{seq}Z",
                        f"{_NEEDLE} {seq}" if seq == 2 else "filler",
                    ),
                )
        db.conn.commit()
    assert main(["search", _NEEDLE, "--json"]) == 0
    hits = json.loads(capsys.readouterr().out)["search"]["hits"]
    assert sorted((h["session_id"], h["position"]) for h in hits) == [(_SID, 3), (other, 3)]
