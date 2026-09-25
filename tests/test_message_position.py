"""`position`: the 1-indexed message number shared by search and inspect.

A search hit's ``position`` must be exactly the number ``inspect`` prints and
``--from-message/--to-message`` accept, so a hit can be quoted in full with
``inspect <sid> --from-message P --to-message P --max-chars 0``.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from convo.cli import main
from convo.db import Database
from convo.read.inspect import _MSG_SELECT, MESSAGE_ORDER_BY

_SID = "5e551011-0000-4000-8000-000000000001"
_NEEDLE = "zebracorn"

# Message row: id, role, seq, timestamp, content, and an optional tool-call input.
type _Msg = tuple[str, str, int, str | None, str, str | None]


def _seed(path: Path, msgs: list[_Msg]) -> None:
    with Database(path) as db:
        assert db.conn is not None
        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (1, '/synthetic/position', 0, 0, '2026-09-01T00:00:00Z')",
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at) "
            "VALUES (?, 1, '/work/position', '2026-09-01T10:00:00Z')",
            (_SID,),
        )
        for mid, role, seq, ts, content, tool_input in msgs:
            db.conn.execute(
                "INSERT INTO messages(id, session_id, role, seq, timestamp, content, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, '{}')",
                (mid, _SID, role, seq, ts, content),
            )
            if tool_input is not None:
                db.conn.execute(
                    "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
                    "started_at) VALUES (?, ?, ?, 0, 'Bash', ?, ?)",
                    (f"tc-{mid}", mid, _SID, tool_input, ts),
                )
                db.conn.execute(
                    "INSERT INTO tool_results(tool_call_id, message_id, output_text) "
                    "VALUES (?, ?, ?)",
                    (f"tc-{mid}", mid, f"output {_NEEDLE}"),
                )
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
        if hit["kind"] == "tool_result":
            assert hit["position"] is None
            continue
        pos = hit["position"]
        assert isinstance(pos, int)
        (msg,) = _json(
            ["inspect", _SID, "--from-message", str(pos), "--to-message", str(pos)], capsys
        )["inspect"]["messages"]
        assert msg["position"] == pos
        if hit["kind"] == "message":
            assert msg["id"] == hit["id"]
        else:
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


@pytest.fixture
def mixed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _seed(live, _MIXED)
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
    by_id = {(h["kind"], h["id"]): h["position"] for h in hits}
    assert by_id == {
        ("message", "m-0"): 1,
        ("tool_call", "tc-m-1"): 2,
        ("tool_result", "tc-m-1"): None,
        ("message", "m-a"): 4,
        ("message", "m-b"): 5,
        ("message", "m-5"): 6,
    }


@pytest.mark.usefixtures("mixed")
def test_search_prose_shows_position(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["search", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "position: 4" in out
    assert main(["search", _NEEDLE, "--fields", "session,position"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"{_SID[:8]}\t4" in lines
    assert f"{_SID[:8]}\t" in lines  # tool_result hit: no position


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
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        live = Path(tmp) / "convo.db"
        mp.setenv("CONVO_DB", str(live))
        _seed(live, msgs)
        _assert_hits_round_trip(capsys)


def test_inspect_select_uses_the_shared_message_order() -> None:
    """Search ranks positions by MESSAGE_ORDER_BY; inspect must list messages the same way."""
    assert _MSG_SELECT.endswith(" ORDER BY " + MESSAGE_ORDER_BY)
