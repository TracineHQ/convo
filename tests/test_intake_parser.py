"""Unit tests for `convo.intake.parser` and `convo.intake.records`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from convo.intake.parser import IntakeParseError, parse_file, parse_line
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
    from pathlib import Path


_USER_STRING = json.dumps(
    {
        "type": "user",
        "uuid": "u-1",
        "parentUuid": None,
        "sessionId": "s-1",
        "timestamp": "2026-04-01T00:00:00Z",
        "message": {"role": "user", "content": "hello world"},
    }
)

_USER_TOOL_RESULT = json.dumps(
    {
        "type": "user",
        "uuid": "u-2",
        "parentUuid": "a-1",
        "sessionId": "s-1",
        "timestamp": "2026-04-01T00:00:01Z",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01",
                    "content": "ok",
                    "is_error": False,
                },
            ],
        },
    }
)

_ASSISTANT_TEXT = json.dumps(
    {
        "type": "assistant",
        "uuid": "a-1",
        "parentUuid": "u-1",
        "sessionId": "s-1",
        "timestamp": "2026-04-01T00:00:02Z",
        "requestId": "req_1",
        "message": {
            "id": "msg_1",
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": "hi"}],
        },
    }
)

_ASSISTANT_MIXED = json.dumps(
    {
        "type": "assistant",
        "uuid": "a-2",
        "parentUuid": "u-2",
        "sessionId": "s-1",
        "timestamp": "2026-04-01T00:00:03Z",
        "requestId": "req_2",
        "message": {
            "id": "msg_2",
            "model": "claude-haiku-4-5-20251001",
            "content": [
                {"type": "thinking", "thinking": "let me think", "signature": "sig"},
                {"type": "text", "text": "answer"},
                {
                    "type": "tool_use",
                    "id": "toolu_02",
                    "name": "Bash",
                    "input": {"command": "ls"},
                },
            ],
        },
    }
)

_ATTACHMENT = json.dumps(
    {
        "type": "attachment",
        "uuid": "att-1",
        "parentUuid": None,
        "sessionId": "s-1",
        "timestamp": "2026-04-01T00:00:04Z",
        "attachment": {
            "type": "hook_success",
            "hookName": "SessionStart:clear",
            "stdout": "ready",
            "exitCode": 0,
        },
    }
)

_QUEUE_OP = json.dumps(
    {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": "2026-04-01T00:00:05Z",
        "sessionId": "s-1",
        "content": "next prompt",
    }
)

_LAST_PROMPT = json.dumps(
    {
        "type": "last-prompt",
        "lastPrompt": "the prompt",
        "sessionId": "s-1",
    }
)

_SYSTEM = json.dumps(
    {
        "type": "system",
        "uuid": "sys-1",
        "parentUuid": "a-2",
        "sessionId": "s-1",
        "timestamp": "2026-04-01T00:00:06Z",
        "subtype": "local_command",
        "content": "<local-command-stdout></local-command-stdout>",
    }
)

_FILE_HISTORY = json.dumps(
    {
        "type": "file-history-snapshot",
        "messageId": "a-2",
        "snapshot": {"messageId": "a-2", "trackedFileBackups": {}},
        "isSnapshotUpdate": False,
    }
)

_UNKNOWN = json.dumps({"type": "future-shape", "foo": "bar"})


def test_parse_user_string_content() -> None:
    rec = parse_line(_USER_STRING)
    assert isinstance(rec, UserMessage)
    assert rec.uuid == "u-1"
    assert rec.parent_uuid is None
    assert rec.session_id == "s-1"
    assert rec.text_content == "hello world"
    assert rec.blocks == ()


def test_parse_user_tool_result_block() -> None:
    rec = parse_line(_USER_TOOL_RESULT)
    assert isinstance(rec, UserMessage)
    assert rec.parent_uuid == "a-1"
    assert rec.text_content is None
    assert len(rec.blocks) == 1
    block = rec.blocks[0]
    assert isinstance(block, ToolResultBlock)
    assert block.tool_use_id == "toolu_01"
    assert block.content == "ok"
    assert block.is_error is False


def test_parse_assistant_text_block() -> None:
    rec = parse_line(_ASSISTANT_TEXT)
    assert isinstance(rec, AssistantMessage)
    assert rec.uuid == "a-1"
    assert rec.model == "claude-haiku-4-5-20251001"
    assert rec.request_id == "req_1"
    assert rec.message_id == "msg_1"
    assert len(rec.blocks) == 1
    assert isinstance(rec.blocks[0], TextBlock)
    assert rec.blocks[0].text == "hi"


def test_parse_assistant_mixed_blocks_yields_three() -> None:
    rec = parse_line(_ASSISTANT_MIXED)
    assert isinstance(rec, AssistantMessage)
    assert len(rec.blocks) == 3
    thinking, text, tool_use = rec.blocks
    assert isinstance(thinking, ThinkingBlock)
    assert thinking.thinking == "let me think"
    assert thinking.signature == "sig"
    assert isinstance(text, TextBlock)
    assert text.text == "answer"
    assert isinstance(tool_use, ToolUseBlock)
    assert tool_use.id == "toolu_02"
    assert tool_use.name == "Bash"
    assert tool_use.input == {"command": "ls"}


def test_parse_attachment() -> None:
    rec = parse_line(_ATTACHMENT)
    assert isinstance(rec, Attachment)
    assert rec.subtype == "hook_success"
    assert rec.payload["stdout"] == "ready"
    assert rec.payload["exitCode"] == 0
    assert rec.parent_uuid is None


def test_parse_queue_operation() -> None:
    rec = parse_line(_QUEUE_OP)
    assert isinstance(rec, QueueOperation)
    assert rec.operation == "enqueue"
    assert rec.content == "next prompt"
    assert rec.session_id == "s-1"


def test_parse_queue_operation_without_content() -> None:
    raw = json.dumps(
        {
            "type": "queue-operation",
            "operation": "dequeue",
            "timestamp": "2026-04-01T00:00:07Z",
            "sessionId": "s-1",
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, QueueOperation)
    assert rec.content is None


def test_parse_last_prompt() -> None:
    rec = parse_line(_LAST_PROMPT)
    assert isinstance(rec, LastPrompt)
    assert rec.last_prompt == "the prompt"
    assert rec.session_id == "s-1"


def test_parse_system_record() -> None:
    rec = parse_line(_SYSTEM)
    assert isinstance(rec, SystemRecord)
    assert rec.subtype == "local_command"
    assert rec.content == "<local-command-stdout></local-command-stdout>"
    assert rec.parent_uuid == "a-2"


def test_parse_system_without_content() -> None:
    raw = json.dumps(
        {
            "type": "system",
            "uuid": "sys-2",
            "parentUuid": None,
            "sessionId": "s-1",
            "timestamp": "2026-04-01T00:00:08Z",
            "subtype": "turn_duration",
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, SystemRecord)
    assert rec.content is None


def test_parse_file_history_snapshot() -> None:
    rec = parse_line(_FILE_HISTORY)
    assert isinstance(rec, FileHistorySnapshot)
    assert rec.message_id == "a-2"
    assert rec.is_snapshot_update is False
    assert rec.snapshot["trackedFileBackups"] == {}


def test_parse_file_history_with_non_dict_snapshot() -> None:
    raw = json.dumps(
        {
            "type": "file-history-snapshot",
            "messageId": "a-3",
            "snapshot": "not-a-dict",
            "isSnapshotUpdate": True,
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, FileHistorySnapshot)
    assert rec.snapshot == {}
    assert rec.is_snapshot_update is True


def test_parse_unknown_top_level_type_returns_unknown_record() -> None:
    rec = parse_line(_UNKNOWN)
    assert isinstance(rec, UnknownRecord)
    assert rec.type_ == "future-shape"
    assert rec.raw["foo"] == "bar"


def test_parse_missing_type_returns_unknown_record() -> None:
    raw = json.dumps({"foo": "bar"})
    rec = parse_line(raw)
    assert isinstance(rec, UnknownRecord)
    assert rec.type_ == "None"


def test_malformed_json_raises_with_lineno() -> None:
    with pytest.raises(IntakeParseError, match="line 7") as excinfo:
        parse_line("{not json", lineno=7)
    assert excinfo.value.lineno == 7
    assert excinfo.value.line == "{not json"


def test_non_object_json_raises() -> None:
    with pytest.raises(IntakeParseError, match="not an object") as excinfo:
        parse_line("[1, 2, 3]", lineno=4)
    assert excinfo.value.lineno == 4


def test_tool_use_with_non_dict_input_falls_back_to_empty() -> None:
    raw = json.dumps(
        {
            "type": "assistant",
            "uuid": "a-9",
            "parentUuid": None,
            "sessionId": "s-1",
            "timestamp": "2026-04-01T00:00:09Z",
            "message": {
                "id": "msg_9",
                "model": "claude-haiku-4-5-20251001",
                "content": [
                    {"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": "bad"},
                ],
            },
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, AssistantMessage)
    block = rec.blocks[0]
    assert isinstance(block, ToolUseBlock)
    assert block.input == {}


def test_tool_result_with_list_content_preserves_list() -> None:
    list_content = [{"type": "text", "text": "nested"}]
    raw = json.dumps(
        {
            "type": "user",
            "uuid": "u-list",
            "parentUuid": "a-1",
            "sessionId": "s-1",
            "timestamp": "2026-04-01T00:00:10Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_list",
                        "content": list_content,
                    },
                ],
            },
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, UserMessage)
    block = rec.blocks[0]
    assert isinstance(block, ToolResultBlock)
    assert block.content == list_content
    assert block.is_error is None


def test_tool_result_with_non_str_non_list_content_coerced_to_str() -> None:
    raw = json.dumps(
        {
            "type": "user",
            "uuid": "u-num",
            "parentUuid": "a-1",
            "sessionId": "s-1",
            "timestamp": "2026-04-01T00:00:11Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_num",
                        "content": 42,
                        "is_error": True,
                    },
                ],
            },
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, UserMessage)
    block = rec.blocks[0]
    assert isinstance(block, ToolResultBlock)
    assert block.content == "42"
    assert block.is_error is True


def test_thinking_block_without_signature() -> None:
    raw = json.dumps(
        {
            "type": "assistant",
            "uuid": "a-no-sig",
            "parentUuid": None,
            "sessionId": "s-1",
            "timestamp": "2026-04-01T00:00:12Z",
            "message": {
                "id": "msg_x",
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "thinking", "thinking": "..."}],
            },
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, AssistantMessage)
    block = rec.blocks[0]
    assert isinstance(block, ThinkingBlock)
    assert block.signature is None


def test_unknown_block_type_is_skipped() -> None:
    raw = json.dumps(
        {
            "type": "assistant",
            "uuid": "a-skip",
            "parentUuid": None,
            "sessionId": "s-1",
            "timestamp": "2026-04-01T00:00:13Z",
            "message": {
                "id": "msg_skip",
                "model": "claude-haiku-4-5-20251001",
                "content": [
                    {"type": "future_block", "data": "x"},
                    {"type": "text", "text": "kept"},
                    "string-not-a-dict",
                ],
            },
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, AssistantMessage)
    assert len(rec.blocks) == 1
    assert isinstance(rec.blocks[0], TextBlock)


def test_user_message_with_non_dict_message_field() -> None:
    raw = json.dumps(
        {
            "type": "user",
            "uuid": "u-bad",
            "parentUuid": None,
            "sessionId": "s-1",
            "timestamp": "2026-04-01T00:00:14Z",
            "message": "not-a-dict",
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, UserMessage)
    assert rec.blocks == ()
    assert rec.text_content is None


def test_assistant_message_with_non_dict_message_field() -> None:
    raw = json.dumps(
        {
            "type": "assistant",
            "uuid": "a-bad",
            "parentUuid": None,
            "sessionId": "s-1",
            "timestamp": "2026-04-01T00:00:15Z",
            "message": "not-a-dict",
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, AssistantMessage)
    assert rec.blocks == ()
    assert rec.message_id is None
    assert rec.model is None


def test_attachment_with_non_dict_inner() -> None:
    raw = json.dumps(
        {
            "type": "attachment",
            "uuid": "att-bad",
            "parentUuid": None,
            "sessionId": "s-1",
            "timestamp": "2026-04-01T00:00:16Z",
            "attachment": "string",
        }
    )
    rec = parse_line(raw)
    assert isinstance(rec, Attachment)
    assert rec.payload == {}
    assert rec.subtype == ""


def test_parse_file_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        f"\n{_USER_STRING}\n   \n{_ASSISTANT_TEXT}\n\n",
        encoding="utf-8",
    )
    records = list(parse_file(path))
    assert len(records) == 2
    assert isinstance(records[0], UserMessage)
    assert isinstance(records[1], AssistantMessage)


def test_parse_file_strips_bom_on_first_line(tmp_path: Path) -> None:
    path = tmp_path / "bom.jsonl"
    path.write_bytes(b"\xef\xbb\xbf" + _USER_STRING.encode("utf-8") + b"\n")
    records = list(parse_file(path))
    assert len(records) == 1
    assert isinstance(records[0], UserMessage)
    assert records[0].uuid == "u-1"


def test_parse_file_propagates_lineno_on_malformed_line(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        f"{_USER_STRING}\n{{broken\n{_ASSISTANT_TEXT}\n",
        encoding="utf-8",
    )
    iterator = parse_file(path)
    first = next(iterator)
    assert isinstance(first, UserMessage)
    with pytest.raises(IntakeParseError, match="line 2") as excinfo:
        next(iterator)
    assert excinfo.value.lineno == 2


def test_parse_file_is_lazy_generator(tmp_path: Path) -> None:
    path = tmp_path / "lazy.jsonl"
    path.write_text(
        f"{_USER_STRING}\n{_ASSISTANT_TEXT}\n{{broken-line\n{_ATTACHMENT}\n",
        encoding="utf-8",
    )
    iterator = parse_file(path)
    first = next(iterator)
    second = next(iterator)
    assert isinstance(first, UserMessage)
    assert isinstance(second, AssistantMessage)


def test_parse_file_empty_file_yields_nothing(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    assert list(parse_file(path)) == []


def test_intake_parse_error_carries_reason() -> None:
    err = IntakeParseError(line="x", lineno=3, reason="bad thing")
    assert err.line == "x"
    assert err.lineno == 3
    assert err.reason == "bad thing"
    assert str(err) == "bad thing"
