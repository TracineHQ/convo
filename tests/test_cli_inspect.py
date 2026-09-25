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
