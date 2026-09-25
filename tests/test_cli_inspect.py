"""Tests for `convo inspect` CLI command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from convo.cli import main
from convo.db import Database

if TYPE_CHECKING:
    from pathlib import Path


_SID = "deadbeef-1111-2222-3333-444455556666"
_LONG_CONTENT = "x" * 500  # > 200 chars, triggers truncation


def _populate(path: Path) -> None:
    """Seed: 1 session, 3 messages, 2 tool calls under the assistant message."""
    with Database(path) as db:
        assert db.conn is not None
        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (1, '/data/foo.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at, ended_at, "
            "model, git_branch) VALUES (?, 1, ?, ?, ?, ?, ?)",
            (
                _SID,
                "/work/foo",
                "2026-04-01T10:00:00Z",
                "2026-04-01T11:00:00Z",
                "claude-opus-4-7",
                "main",
            ),
        )
        msgs = [
            ("m1", "user", 0, "2026-04-01T10:00:00Z", "what does ls do?"),
            ("m2", "assistant", 1, "2026-04-01T10:00:30Z", _LONG_CONTENT),
            ("m3", "user", 2, "2026-04-01T10:01:00Z", "thanks"),
        ]
        for mid, role, seq, ts, content in msgs:
            db.conn.execute(
                "INSERT INTO messages(id, session_id, role, seq, timestamp, content, "
                "raw_json) VALUES (?, ?, ?, ?, ?, ?, '{}')",
                (mid, _SID, role, seq, ts, content),
            )
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
            "started_at) VALUES ('tc1', 'm2', ?, 0, 'Bash', "
            "'{\"command\": \"ls /tmp\"}', '2026-04-01T10:00:31Z')",
            (_SID,),
        )
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
            "started_at) VALUES ('tc2', 'm2', ?, 1, 'Read', "
            "'{\"path\": \"/tmp/foo.txt\"}', '2026-04-01T10:00:32Z')",
            (_SID,),
        )
        db.conn.commit()


def test_inspect_prose_header_and_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", _SID])
    assert rc == 0
    out = capsys.readouterr().out
    assert _SID in out
    assert "/work/foo" in out
    assert "claude-opus-4-7" in out
    assert "main" in out
    # Timeline numbering + role icons.
    assert "1. U:" in out
    assert "2. A:" in out
    assert "3. U:" in out
    # Tool-call inline lines under the assistant message.
    assert "  → Bash:" in out
    assert "  → Read:" in out
    # Default truncation: long content gets cut off with "...".
    assert "..." in out
    assert "x" * 500 not in out


def test_inspect_full_no_message_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # v2: --full now means "no message cap" (excerpt truncation still applies)
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", _SID, "--full"])
    assert rc == 0
    out = capsys.readouterr().out
    # Session has 3 messages; --full returns all (no cap), no truncation footer.
    assert "(showing" not in out
    assert "use --full for all" not in out


def test_inspect_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", _SID, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == 2  # v2 envelope
    assert "inspect" in payload
    block = payload["inspect"]
    assert block["session"]["id"] == _SID
    assert block["session"]["project_path"] == "/work/foo"
    assert block["session"]["model"] == "claude-opus-4-7"
    assert block["session"]["git_branch"] == "main"
    assert len(block["messages"]) == 3
    # Session has 3 messages (< 50 cap); not truncated at session level.
    assert block["truncated"] is False

    # Per-message: long content is still excerpt-truncated to 200 chars.
    assistant_msg = next(m for m in block["messages"] if m["id"] == "m2")
    assert len(assistant_msg["content"]) == 200 + len("...")
    assert assistant_msg["truncated"] is True
    assert len(assistant_msg["tool_calls"]) == 2
    assert assistant_msg["tool_calls"][0]["name"] == "Bash"
    assert assistant_msg["tool_calls"][1]["name"] == "Read"

    # Short message is not truncated.
    user_msg = next(m for m in block["messages"] if m["id"] == "m1")
    assert user_msg["truncated"] is False
    assert user_msg["content"] == "what does ls do?"


def test_inspect_full_json_no_message_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # v2: --full now means "no message cap"; envelope truncated field is False
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", _SID, "--full", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    block = payload["inspect"]
    assert block["truncated"] is False
    # Session has 3 messages; all returned
    assert len(block["messages"]) == 3


def test_inspect_unique_prefix_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", "deadbeef", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["inspect"]["session"]["id"] == _SID


def test_inspect_no_match_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", "zzzznope"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.startswith("convo:")
    assert "no session matches zzzznope" in captured.err


def test_inspect_ambiguous_lists_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    # Two sessions sharing a prefix.
    with Database(live) as db:
        assert db.conn is not None
        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (1, '/data/a.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
        )
        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (2, '/data/b.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id) VALUES ('abcd1111-x', 1)",
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id) VALUES ('abcd2222-y', 2)",
        )
        db.conn.commit()

    rc = main(["inspect", "abcd"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "ambiguous" in captured.err
    assert "abcd1111" in captured.err
    assert "abcd2222" in captured.err


def test_inspect_help_lists_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["inspect", "--help"])
    out = capsys.readouterr().out
    assert "--full" in out
    assert "--json" in out


def test_top_level_help_lists_inspect(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "inspect" in out


def test_inspect_json_error_envelope_on_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --json, modeled errors emit a JSON error envelope on stdout."""
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", "no-such-session", "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 2  # v2 envelope
    assert isinstance(payload["error"]["message"], str)
    assert payload["error"]["message"]


def test_inspect_latest_on_populated_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`convo inspect --latest` resolves to the newest started_at session."""
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)

    rc = main(["inspect", "--latest", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # _populate seeds a single session whose id is _SID; --latest must pick it.
    assert payload["inspect"]["session"]["id"] == _SID


def test_inspect_latest_empty_db_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`convo inspect --latest` on an empty DB exits 1 with `no sessions in DB`."""
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    # Bootstrap the schema but insert nothing.
    with Database(live):
        pass

    rc = main(["inspect", "--latest"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "convo: no sessions in DB" in captured.err


def test_inspect_no_target_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """`convo inspect` with neither session_id nor --latest exits 2."""
    with pytest.raises(SystemExit) as excinfo:
        main(["inspect"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "one of the arguments" in err or "required" in err


# ---------------------------------------------------------------------------
# --max-chars, --from-message/--to-message, --timeline --json
# ---------------------------------------------------------------------------

_MANY_SID = "cafef00d-aaaa-bbbb-cccc-ddddeeeeffff"


def _populate_many(path: Path, n: int, contents: dict[int, str] | None = None) -> None:
    """Seed one session with `n` messages; message i (1-indexed) says `msg-i`."""
    contents = contents or {}
    with Database(path) as db:
        assert db.conn is not None
        db.conn.execute(
            "INSERT INTO source_files(id, path, size, mtime_ns, last_indexed_at) "
            "VALUES (1, '/data/many.jsonl', 0, 0, '2026-04-29T00:00:00Z')",
        )
        db.conn.execute(
            "INSERT INTO sessions(id, source_file_id, project_path, started_at) "
            "VALUES (?, 1, '/work/many', '2026-04-01T10:00:00Z')",
            (_MANY_SID,),
        )
        for i in range(1, n + 1):
            db.conn.execute(
                "INSERT INTO messages(id, session_id, role, seq, timestamp, content, "
                "raw_json) VALUES (?, ?, 'user', ?, ?, ?, '{}')",
                (
                    f"n{i}",
                    _MANY_SID,
                    i,
                    f"2026-04-01T10:{i // 60:02d}:{i % 60:02d}Z",
                    contents.get(i, f"msg-{i}"),
                ),
            )
        db.conn.commit()


def _inspect_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    rc = main(["inspect", *argv, "--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    payload: dict[str, Any] = json.loads(out)
    return payload


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    _populate(live)
    return live


@pytest.fixture
def many(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    live = tmp_path / "convo.db"
    monkeypatch.setenv("CONVO_DB", str(live))
    return live


@pytest.mark.usefixtures("seeded")
def test_inspect_max_chars_zero_returns_full_content_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: content was always clipped to 200 chars with no way out."""
    block = _inspect_json([_SID, "--max-chars", "0"], capsys)["inspect"]
    m2 = next(m for m in block["messages"] if m["id"] == "m2")
    assert m2["content"] == _LONG_CONTENT
    assert m2["truncated"] is False


@pytest.mark.usefixtures("seeded")
def test_inspect_max_chars_zero_returns_full_content_prose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["inspect", _SID, "--max-chars", "0"]) == 0
    out = capsys.readouterr().out
    assert _LONG_CONTENT in out
    assert _LONG_CONTENT + "..." not in out


@pytest.mark.usefixtures("seeded")
def test_inspect_max_chars_custom_limit(capsys: pytest.CaptureFixture[str]) -> None:
    block = _inspect_json([_SID, "--max-chars", "10"], capsys)["inspect"]
    by_id = {m["id"]: m for m in block["messages"]}
    assert by_id["m2"]["content"] == "x" * 10 + "..."
    assert by_id["m2"]["truncated"] is True
    assert by_id["m1"]["content"] == "what does ..."
    assert by_id["m1"]["truncated"] is True
    assert by_id["m3"]["content"] == "thanks"
    assert by_id["m3"]["truncated"] is False


@pytest.mark.usefixtures("seeded")
def test_inspect_max_chars_exact_length_not_truncated(capsys: pytest.CaptureFixture[str]) -> None:
    block = _inspect_json([_SID, "--max-chars", "500"], capsys)["inspect"]
    m2 = next(m for m in block["messages"] if m["id"] == "m2")
    assert m2["content"] == _LONG_CONTENT
    assert m2["truncated"] is False


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("bad", ["-1", "abc"])
def test_inspect_max_chars_rejects_invalid(capsys: pytest.CaptureFixture[str], bad: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["inspect", _SID, "--max-chars", bad])
    assert excinfo.value.code == 2
    assert "--max-chars must be 0 (no limit) or a positive integer" in capsys.readouterr().err


def test_inspect_max_chars_empty_content(many: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _populate_many(many, 1, {1: ""})
    for limit in ("0", "5"):
        block = _inspect_json([_MANY_SID, "--max-chars", limit], capsys)["inspect"]
        assert block["messages"][0]["content"] == ""
        assert block["messages"][0]["truncated"] is False


def test_inspect_max_chars_multibyte_boundary(
    many: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Limits count code points: the cut never splits a multibyte character."""
    text = "abé\U0001f600漢字end"  # a b e-acute grinning-face kanji kanji e n d
    _populate_many(many, 1, {1: text})
    for n in range(1, len(text)):
        block = _inspect_json([_MANY_SID, "--max-chars", str(n)], capsys)["inspect"]
        assert block["messages"][0]["content"] == text[:n] + "..."
        assert block["messages"][0]["truncated"] is True
        assert main(["inspect", _MANY_SID, "--max-chars", str(n)]) == 0
        out = capsys.readouterr().out
        assert f"{text[:n]}..." in out
        out.encode("utf-8")  # no lone surrogates


@pytest.mark.usefixtures("seeded")
def test_inspect_timeline_max_chars(capsys: pytest.CaptureFixture[str]) -> None:
    # Default timeline preview is unchanged: 80 chars, no ellipsis.
    assert main(["inspect", _SID, "--timeline"]) == 0
    out = capsys.readouterr().out
    assert "x" * 80 in out
    assert "x" * 81 not in out

    assert main(["inspect", _SID, "--timeline", "--max-chars", "0"]) == 0
    assert _LONG_CONTENT in capsys.readouterr().out

    assert main(["inspect", _SID, "--timeline", "--max-chars", "5"]) == 0
    out = capsys.readouterr().out
    assert "xxxxx" in out
    assert "xxxxxx" not in out
    assert "what " in out
    assert "what d" not in out


_LONG_INPUT = '{"command": "' + "y" * 100 + '"}'


def _add_long_tool_call(path: Path) -> None:
    """Attach a tool call with a 115-char input to message m3 of `_populate`."""
    with Database(path) as db:
        assert db.conn is not None
        db.conn.execute(
            "INSERT INTO tool_calls(id, message_id, session_id, seq, name, input_json, "
            "started_at) VALUES ('tc3', 'm3', ?, 0, 'Bash', ?, '2026-04-01T10:01:01Z')",
            (_SID, _LONG_INPUT),
        )
        db.conn.commit()


def test_inspect_max_chars_applies_to_tool_inputs(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_long_tool_call(seeded)

    # Prose default: 80 chars plus "...".
    assert main(["inspect", _SID]) == 0
    out = capsys.readouterr().out
    assert f"  → Bash: {_LONG_INPUT[:80]}..." in out

    assert main(["inspect", _SID, "--max-chars", "0"]) == 0
    assert f"  → Bash: {_LONG_INPUT}\n" in capsys.readouterr().out

    assert main(["inspect", _SID, "--max-chars", "5"]) == 0
    assert '  → Bash: {"com...\n' in capsys.readouterr().out

    def calls(*extra: str) -> dict[str, dict[str, Any]]:
        msgs = _inspect_json([_SID, *extra], capsys)["inspect"]["messages"]
        return {tc["id"]: tc for m in msgs for tc in m["tool_calls"]}

    # JSON default: input_json is complete, as before.
    tcs = calls()
    assert tcs["tc3"]["input_json"] == _LONG_INPUT
    assert tcs["tc3"]["truncated"] is False

    tcs = calls("--max-chars", "5")
    assert tcs["tc3"]["input_json"] == '{"com...'
    assert tcs["tc3"]["truncated"] is True
    assert tcs["tc1"]["truncated"] is True

    tcs = calls("--max-chars", "0")
    assert tcs["tc3"]["input_json"] == _LONG_INPUT
    assert tcs["tc3"]["truncated"] is False


def test_inspect_range_applies_without_timeline(
    many: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: --from-message/--to-message were ignored unless --timeline."""
    _populate_many(many, 5)
    inner = _inspect_json([_MANY_SID, "--from-message", "2", "--to-message", "3"], capsys)[
        "inspect"
    ]
    assert [m["content"] for m in inner["messages"]] == ["msg-2", "msg-3"]
    assert inner["total_messages"] == 5
    assert inner["truncated"] is False

    # Prose numbers messages by their position in the session.
    assert main(["inspect", _MANY_SID, "--from-message", "2", "--to-message", "3"]) == 0
    out = capsys.readouterr().out
    assert "\n2. U:" in out
    assert "\n3. U:" in out
    assert "\n1. U:" not in out
    assert "msg-4" not in out


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--from-message", "4"], ["msg-4", "msg-5"]),
        (["--to-message", "2"], ["msg-1", "msg-2"]),
        (["--from-message", "5", "--to-message", "5"], ["msg-5"]),
        (["--from-message", "4", "--to-message", "99"], ["msg-4", "msg-5"]),
        # More than 50 past the end: an unclamped end would trip the 50-message cap.
        (["--to-message", "70"], ["msg-1", "msg-2", "msg-3", "msg-4", "msg-5"]),
    ],
)
def test_inspect_range_open_ends_and_clamp(
    many: Path, capsys: pytest.CaptureFixture[str], argv: list[str], expected: list[str]
) -> None:
    _populate_many(many, 5)
    inner = _inspect_json([_MANY_SID, *argv], capsys)["inspect"]
    assert [m["content"] for m in inner["messages"]] == expected
    assert inner["truncated"] is False


@pytest.mark.parametrize("timeline", [False, True])
@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--from-message", "3", "--to-message", "2"], "greater than --to-message"),
        (["--from-message", "6"], "beyond the last message (session has 5)"),
    ],
)
def test_inspect_range_errors(
    many: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
    timeline: bool,  # noqa: FBT001
) -> None:
    _populate_many(many, 5)
    extra = ["--timeline"] if timeline else []

    assert main(["inspect", _MANY_SID, *argv, *extra]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err

    assert main(["inspect", _MANY_SID, *argv, *extra, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    assert message in payload["error"]["message"]


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize("flag", ["--from-message", "--to-message"])
@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_inspect_range_rejects_non_positive(
    capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["inspect", _SID, flag, value])
    assert excinfo.value.code == 2
    assert "1-indexed" in capsys.readouterr().err


def test_inspect_range_with_message_cap(many: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _populate_many(many, 62)

    inner = _inspect_json([_MANY_SID, "--from-message", "2"], capsys)["inspect"]
    assert len(inner["messages"]) == 50
    assert inner["messages"][0]["content"] == "msg-2"
    assert inner["truncated"] is True

    inner = _inspect_json([_MANY_SID, "--from-message", "2", "--full"], capsys)["inspect"]
    assert len(inner["messages"]) == 61
    assert inner["truncated"] is False

    inner = _inspect_json([_MANY_SID, "--from-message", "20"], capsys)["inspect"]
    assert len(inner["messages"]) == 43
    assert inner["truncated"] is False


def test_inspect_range_prose_footer_describes_window(
    many: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _populate_many(many, 62)
    assert main(["inspect", _MANY_SID, "--from-message", "2", "--to-message", "60"]) == 0
    out = capsys.readouterr().out
    assert "(showing messages 2-51 of the selected 2-60; use --full for all)" in out

    # Without a range the footer keeps its whole-session wording.
    assert main(["inspect", _MANY_SID]) == 0
    assert "(showing 50 of 62 messages; use --full for all)" in capsys.readouterr().out


def test_inspect_json_echoes_range(many: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _populate_many(many, 5)
    inner = _inspect_json([_MANY_SID, "--from-message", "2", "--to-message", "9"], capsys)[
        "inspect"
    ]
    assert inner["from_message"] == 2
    assert inner["to_message"] == 9
    inner = _inspect_json([_MANY_SID], capsys)["inspect"]
    assert inner["from_message"] is None
    assert inner["to_message"] is None
