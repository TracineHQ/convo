"""Unit + round-trip tests for `convo.intake.mapper`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from convo.intake.mapper import INSERT_SQL, map_record
from convo.intake.records import (
    AssistantMessage,
    Attachment,
    FileHistorySnapshot,
    LastPrompt,
    QueueOperation,
    SystemRecord,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UnknownRecord,
    UserMessage,
)

if TYPE_CHECKING:
    from convo.db import Database
    from convo.intake.records import IntakeRecord


_NOW = "2026-04-29T00:00:00Z"
_SID = "session-1"


def _fresh_counter() -> dict[str, int]:
    return {"messages": 0, "tool_calls": 0}


def _user_string(uuid: str = "u-1", content: str = "hello world") -> UserMessage:
    return UserMessage(
        uuid=uuid,
        parent_uuid=None,
        session_id=_SID,
        timestamp=_NOW,
        blocks=(),
        text_content=content,
        raw={"type": "user", "content": content},
    )


def _user_with_tool_result(
    *,
    uuid: str = "u-2",
    parent: str | None = "a-1",
    tool_use_id: str = "toolu_01",
    content: str | list[dict[str, object]] = "ok",
    is_error: bool | None = False,
) -> UserMessage:
    block = ToolResultBlock(
        tool_use_id=tool_use_id,
        content=content,
        is_error=is_error,
        raw={"type": "tool_result"},
    )
    return UserMessage(
        uuid=uuid,
        parent_uuid=parent,
        session_id=_SID,
        timestamp=_NOW,
        blocks=(block,),
        text_content=None,
        raw={"type": "user", "uuid": uuid},
    )


def _assistant(
    *,
    uuid: str = "a-1",
    blocks: tuple[object, ...] = (),
    parent: str | None = "u-1",
) -> AssistantMessage:
    return AssistantMessage(
        uuid=uuid,
        parent_uuid=parent,
        session_id=_SID,
        timestamp=_NOW,
        model="claude-haiku-4-5",
        request_id="req_1",
        message_id="msg_1",
        blocks=blocks,  # type: ignore[arg-type]
        raw={"type": "assistant", "uuid": uuid},
    )


def _collect(
    rec: IntakeRecord,
    counter: dict[str, int],
    *,
    message_ids: frozenset[str] = frozenset(),
    tool_call_ids: frozenset[str] = frozenset(),
) -> list[tuple[str, tuple[object, ...]]]:
    return list(
        map_record(
            rec,
            session_id=_SID,
            source_file_id=1,
            seq_counter=counter,
            existing_message_ids=message_ids,
            existing_tool_call_ids=tool_call_ids,
        ),
    )


def test_user_string_yields_one_messages_row() -> None:
    counter = _fresh_counter()
    rows = _collect(_user_string(content="hi there"), counter)
    assert len(rows) == 1
    table, tup = rows[0]
    assert table == "messages"
    assert tup[0] == "u-1"
    assert tup[1] == _SID
    assert tup[2] is None
    assert tup[3] == "user"
    assert tup[4] == 0
    assert tup[5] == _NOW
    assert tup[6] == "hi there"
    assert tup[7] == 0
    assert json.loads(cast("str", tup[8]))["type"] == "user"
    assert counter == {"messages": 1, "tool_calls": 0}


def test_user_with_tool_result_yields_message_plus_result() -> None:
    counter = _fresh_counter()
    rec = _user_with_tool_result(content="output here", is_error=True)
    rows = _collect(rec, counter, tool_call_ids=frozenset({"toolu_01"}))
    assert len(rows) == 2
    msg_table, msg_row = rows[0]
    res_table, res_row = rows[1]
    assert msg_table == "messages"
    assert msg_row[3] == "user"
    assert msg_row[6] == ""
    assert msg_row[7] == 0
    assert res_table == "tool_results"
    assert res_row[0] == "toolu_01"
    assert res_row[1] == msg_row[0]
    assert res_row[2] == 1
    assert res_row[3] == "output here"


def test_user_with_tool_result_list_content_serialized_as_json() -> None:
    counter = _fresh_counter()
    list_content: list[dict[str, object]] = [{"type": "text", "text": "nested"}]
    rec = _user_with_tool_result(content=list_content, is_error=None)
    rows = _collect(rec, counter, tool_call_ids=frozenset({"toolu_01"}))
    res_row = rows[1][1]
    assert res_row[2] == 0
    assert json.loads(cast("str", res_row[3])) == list_content


def test_assistant_text_only_yields_one_messages_row() -> None:
    counter = _fresh_counter()
    rec = _assistant(blocks=(TextBlock(text="answer"),))
    rows = _collect(rec, counter)
    assert len(rows) == 1
    table, tup = rows[0]
    assert table == "messages"
    assert tup[3] == "assistant"
    assert tup[6] == "answer"
    assert counter == {"messages": 1, "tool_calls": 0}


def test_assistant_text_plus_tool_use_yields_message_and_call() -> None:
    counter = _fresh_counter()
    rec = _assistant(
        blocks=(
            TextBlock(text="here"),
            ToolUseBlock(id="toolu_x", name="Bash", input={"command": "ls"}),
        ),
    )
    rows = _collect(rec, counter)
    assert len(rows) == 2
    msg_row = rows[0][1]
    tc_table, tc_row = rows[1]
    assert rows[0][0] == "messages"
    assert msg_row[6] == "here"
    assert msg_row[7] == 0
    assert tc_table == "tool_calls"
    assert tc_row[0] == "toolu_x"
    assert tc_row[1] == msg_row[0]
    assert tc_row[2] == _SID
    assert tc_row[3] == 0
    assert tc_row[4] == "Bash"
    assert json.loads(cast("str", tc_row[5])) == {"command": "ls"}
    assert tc_row[6] == _NOW
    assert tc_row[7] == _NOW
    assert tc_row[8] is None
    assert tc_row[9] == 0
    assert counter == {"messages": 1, "tool_calls": 1}


def test_assistant_thinking_text_tool_use_concatenates_content() -> None:
    counter = _fresh_counter()
    rec = _assistant(
        blocks=(
            ThinkingBlock(thinking="reasoning", signature="sig"),
            TextBlock(text="answer"),
            ToolUseBlock(id="toolu_y", name="Read", input={"path": "/a"}),
        ),
    )
    rows = _collect(rec, counter)
    assert len(rows) == 2
    msg_row = rows[0][1]
    assert msg_row[6] == "reasoning\nanswer"
    assert msg_row[7] == 1
    tc_row = rows[1][1]
    assert tc_row[4] == "Read"


def test_assistant_input_without_real_newlines_clears_multiline_flag() -> None:
    counter = _fresh_counter()
    rec = _assistant(
        blocks=(ToolUseBlock(id="toolu_z", name="Bash", input={"command": "echo a\nb"}),),
    )
    rows = _collect(rec, counter)
    tc_row = rows[1][1]
    assert tc_row[9] == 0


def test_assistant_message_content_with_newlines_sets_message_flag() -> None:
    counter = _fresh_counter()
    rec = _assistant(
        blocks=(
            TextBlock(text="line1"),
            TextBlock(text="line2"),
        ),
    )
    rows = _collect(rec, counter)
    msg_row = rows[0][1]
    assert msg_row[6] == "line1\nline2"
    assert msg_row[7] == 1


def test_synthesizes_message_id_when_uuid_blank() -> None:
    counter = _fresh_counter()
    rec = UserMessage(
        uuid="",
        parent_uuid=None,
        session_id=_SID,
        timestamp=_NOW,
        blocks=(),
        text_content="x",
        raw={"type": "user"},
    )
    rows = _collect(rec, counter)
    msg_id = rows[0][1][0]
    assert isinstance(msg_id, str)
    assert len(msg_id) == 36


def test_synthesizes_tool_call_id_when_block_id_blank() -> None:
    counter = _fresh_counter()
    rec = _assistant(
        blocks=(ToolUseBlock(id="", name="Bash", input={"command": "ls"}),),
    )
    rows = _collect(rec, counter)
    tc_id = rows[1][1][0]
    assert isinstance(tc_id, str)
    assert len(tc_id) == 36


def test_drop_attachment_yields_nothing() -> None:
    counter = _fresh_counter()
    rec = Attachment(
        uuid="att-1",
        parent_uuid=None,
        session_id=_SID,
        timestamp=_NOW,
        subtype="hook_success",
        payload={},
        raw={"type": "attachment"},
    )
    assert _collect(rec, counter) == []
    assert counter == {"messages": 0, "tool_calls": 0}


def test_drop_queue_operation() -> None:
    counter = _fresh_counter()
    rec = QueueOperation(
        session_id=_SID,
        timestamp=_NOW,
        operation="enqueue",
        content="x",
        raw={"type": "queue-operation"},
    )
    assert _collect(rec, counter) == []


def test_drop_last_prompt() -> None:
    counter = _fresh_counter()
    rec = LastPrompt(session_id=_SID, last_prompt="x", raw={"type": "last-prompt"})
    assert _collect(rec, counter) == []


def test_drop_system_record() -> None:
    counter = _fresh_counter()
    rec = SystemRecord(
        uuid="sys-1",
        parent_uuid=None,
        session_id=_SID,
        timestamp=_NOW,
        subtype="local_command",
        content=None,
        raw={"type": "system"},
    )
    assert _collect(rec, counter) == []


def test_drop_file_history_snapshot() -> None:
    counter = _fresh_counter()
    rec = FileHistorySnapshot(
        message_id="m-1",
        snapshot={},
        is_snapshot_update=False,
        raw={"type": "file-history-snapshot"},
    )
    assert _collect(rec, counter) == []


def test_drop_unknown_record() -> None:
    counter = _fresh_counter()
    rec = UnknownRecord(type_="future", raw={"type": "future"})
    assert _collect(rec, counter) == []


def test_seq_counter_bumped_across_mixed_records() -> None:
    counter = _fresh_counter()
    rows1 = _collect(_user_string(uuid="u-a", content="one"), counter)
    rows2 = _collect(
        _assistant(
            uuid="a-a",
            blocks=(
                TextBlock(text="t"),
                ToolUseBlock(id="t1", name="Bash", input={}),
                ToolUseBlock(id="t2", name="Read", input={}),
            ),
        ),
        counter,
    )
    rows3 = _collect(_user_string(uuid="u-b", content="two"), counter)
    assert rows1[0][1][4] == 0
    assert rows2[0][1][4] == 1
    assert rows3[0][1][4] == 2
    assert rows2[1][1][3] == 0
    assert rows2[2][1][3] == 1
    assert counter == {"messages": 3, "tool_calls": 2}


def test_user_with_non_tool_result_blocks_skips_them() -> None:
    counter = _fresh_counter()
    rec = UserMessage(
        uuid="u-mix",
        parent_uuid=None,
        session_id=_SID,
        timestamp=_NOW,
        blocks=(
            TextBlock(text="kept"),
            ToolResultBlock(tool_use_id="t1", content="r", is_error=False),
        ),
        text_content=None,
        raw={"type": "user"},
    )
    rows = _collect(rec, counter, tool_call_ids=frozenset({"t1"}))
    assert len(rows) == 2
    assert rows[0][0] == "messages"
    assert rows[0][1][6] == "kept"
    assert rows[1][0] == "tool_results"


def test_user_message_with_newlines_flag() -> None:
    counter = _fresh_counter()
    rec = _user_string(content="line1\nline2")
    rows = _collect(rec, counter)
    assert rows[0][1][7] == 1


def test_insert_sql_keys_match_yielded_tables() -> None:
    assert set(INSERT_SQL) == {"messages", "tool_calls", "tool_results"}


def test_round_trip_inserts_and_selects(db: Database) -> None:
    assert db.conn is not None
    db.conn.execute(
        "INSERT INTO source_files(path, size, mtime_ns, last_indexed_at) VALUES (?, 0, 0, ?)",
        ("/data/x.jsonl", _NOW),
    )
    sfid = db.conn.execute("SELECT id FROM source_files").fetchone()[0]
    db.conn.execute(
        "INSERT INTO sessions(id, source_file_id) VALUES (?, ?)",
        (_SID, sfid),
    )
    counter = _fresh_counter()
    records: list[IntakeRecord] = [
        _assistant(
            uuid="a-rt",
            parent=None,
            blocks=(
                TextBlock(text="reply"),
                ToolUseBlock(id="toolu_rt", name="Bash", input={"command": "ls -la"}),
            ),
        ),
        _user_with_tool_result(
            uuid="u-rt",
            parent="a-rt",
            tool_use_id="toolu_rt",
            content="exit 0",
            is_error=False,
        ),
    ]
    msg_ids = frozenset({"a-rt", "u-rt"})
    tc_ids = frozenset({"toolu_rt"})
    for rec in records:
        for table, row in map_record(
            rec,
            session_id=_SID,
            source_file_id=sfid,
            seq_counter=counter,
            existing_message_ids=msg_ids,
            existing_tool_call_ids=tc_ids,
        ):
            db.conn.execute(INSERT_SQL[table], row)
    db.conn.commit()

    msg_rows = db.conn.execute(
        "SELECT id, role, seq, content, has_newlines FROM messages ORDER BY seq",
    ).fetchall()
    assert len(msg_rows) == 2
    assert msg_rows[0]["id"] == "a-rt"
    assert msg_rows[0]["role"] == "assistant"
    assert msg_rows[0]["content"] == "reply"
    assert msg_rows[1]["id"] == "u-rt"
    assert msg_rows[1]["role"] == "user"
    assert msg_rows[1]["content"] == ""

    tc_rows = db.conn.execute(
        "SELECT id, message_id, name, input_json, started_at FROM tool_calls",
    ).fetchall()
    assert len(tc_rows) == 1
    assert tc_rows[0]["id"] == "toolu_rt"
    assert tc_rows[0]["message_id"] == "a-rt"
    assert tc_rows[0]["name"] == "Bash"
    assert json.loads(tc_rows[0]["input_json"]) == {"command": "ls -la"}
    assert tc_rows[0]["started_at"] == _NOW

    tr_rows = db.conn.execute(
        "SELECT tool_call_id, message_id, is_error, output_text FROM tool_results",
    ).fetchall()
    assert len(tr_rows) == 1
    assert tr_rows[0]["tool_call_id"] == "toolu_rt"
    assert tr_rows[0]["message_id"] == "u-rt"
    assert tr_rows[0]["is_error"] == 0
    assert tr_rows[0]["output_text"] == "exit 0"

    assert counter == {"messages": 2, "tool_calls": 1}
