"""Map `IntakeRecord` instances onto schema row tuples.

Pure function: takes a parsed record plus session context and yields
`(table_name, row_tuple)` pairs in column order matching `INSERT_SQL[table_name]`.
The orchestrator wires the output to `executemany`.

Mapping rules:

* `UserMessage` with string content   -> 1 messages row
* `UserMessage` with list content     -> 1 messages row
                                       + 1 tool_results row per tool_result block
* `AssistantMessage`                  -> 1 messages row
                                       + 1 tool_calls row per tool_use block
                                         (text + thinking blocks concatenated
                                         into the message's content column)
* `Attachment` / `QueueOperation`     -> dropped
* `LastPrompt` / `SystemRecord`       -> dropped
* `FileHistorySnapshot`               -> dropped
* `UnknownRecord`                     -> dropped

`tool_results` carries `(tool_call_id PK, message_id, is_error, output_text)`;
list/dict payloads are JSON-encoded into `output_text`.
"""

from __future__ import annotations

import json
import uuid as _uuid
from typing import TYPE_CHECKING, Any, Final

from convo.intake.records import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from convo.intake.records import IntakeRecord, MessageBlock

INSERT_SQL: Final[dict[str, str]] = {
    "messages": (
        "INSERT OR IGNORE INTO messages("
        "id, session_id, parent_id, role, seq, timestamp, content, has_newlines, raw_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ),
    "tool_calls": (
        "INSERT OR IGNORE INTO tool_calls("
        "id, message_id, session_id, seq, name, input_json, "
        "started_at, ended_at, duration_ms, has_newlines"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ),
    "tool_results": (
        "INSERT OR IGNORE INTO tool_results("
        "tool_call_id, message_id, is_error, output_text"
        ") VALUES (?, ?, ?, ?)"
    ),
}

_NAMESPACE = _uuid.NAMESPACE_DNS


def _has_nl(text: str) -> int:
    return 1 if "\n" in text else 0


def _synth_id(session_id: str, key: str, seq: int) -> str:
    return str(_uuid.uuid5(_NAMESPACE, f"{session_id}:{key}:{seq}"))


def _serialize_result_content(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _assistant_text(blocks: tuple[MessageBlock, ...]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ThinkingBlock):
            parts.append(block.thinking)
    return "\n".join(parts)


def _user_text(record: UserMessage) -> str:
    if record.text_content is not None:
        return record.text_content
    parts = [block.text for block in record.blocks if isinstance(block, TextBlock)]
    return "\n".join(parts)


def _message_id(record_uuid: str, session_id: str, seq: int) -> str:
    if record_uuid:
        return record_uuid
    return _synth_id(session_id, "message", seq)


def _tool_call_id(block_id: str, session_id: str, seq: int) -> str:
    if block_id:
        return block_id
    return _synth_id(session_id, "tool_call", seq)


def _map_user(
    record: UserMessage,
    *,
    session_id: str,
    seq_counter: dict[str, int],
    existing_message_ids: frozenset[str],
    existing_tool_call_ids: frozenset[str],
) -> Iterator[tuple[str, tuple[Any, ...]]]:
    msg_seq = seq_counter["messages"]
    seq_counter["messages"] = msg_seq + 1
    msg_id = _message_id(record.uuid, session_id, msg_seq)
    content = _user_text(record)
    raw_json = json.dumps(record.raw, ensure_ascii=False, sort_keys=True)
    parent_id = record.parent_uuid if record.parent_uuid in existing_message_ids else None
    yield (
        "messages",
        (
            msg_id,
            session_id,
            parent_id,
            "user",
            msg_seq,
            record.timestamp or None,
            content,
            _has_nl(content),
            raw_json,
        ),
    )
    for block in record.blocks:
        if not isinstance(block, ToolResultBlock):
            continue
        # Drop tool_results whose referenced tool_call_id isn't in this file —
        # the parent tool_use lives in a session we resumed from and isn't a
        # `tool_calls` row here.
        if block.tool_use_id not in existing_tool_call_ids:
            continue
        output_text = _serialize_result_content(block.content)
        is_error = 1 if block.is_error else 0
        yield (
            "tool_results",
            (
                block.tool_use_id,
                msg_id,
                is_error,
                output_text,
            ),
        )


def _map_assistant(
    record: AssistantMessage,
    *,
    session_id: str,
    seq_counter: dict[str, int],
    existing_message_ids: frozenset[str],
) -> Iterator[tuple[str, tuple[Any, ...]]]:
    msg_seq = seq_counter["messages"]
    seq_counter["messages"] = msg_seq + 1
    msg_id = _message_id(record.uuid, session_id, msg_seq)
    content = _assistant_text(record.blocks)
    raw_json = json.dumps(record.raw, ensure_ascii=False, sort_keys=True)
    parent_id = record.parent_uuid if record.parent_uuid in existing_message_ids else None
    yield (
        "messages",
        (
            msg_id,
            session_id,
            parent_id,
            "assistant",
            msg_seq,
            record.timestamp or None,
            content,
            _has_nl(content),
            raw_json,
        ),
    )
    timestamp = record.timestamp or None
    for block in record.blocks:
        if not isinstance(block, ToolUseBlock):
            continue
        tc_seq = seq_counter["tool_calls"]
        seq_counter["tool_calls"] = tc_seq + 1
        input_json = json.dumps(block.input, ensure_ascii=False, sort_keys=True)
        yield (
            "tool_calls",
            (
                _tool_call_id(block.id, session_id, tc_seq),
                msg_id,
                session_id,
                tc_seq,
                block.name,
                input_json,
                timestamp,
                timestamp,
                None,
                _has_nl(input_json),
            ),
        )


def map_record(  # noqa: PLR0913 — context bag is the FK contract; see docstring
    record: IntakeRecord,
    *,
    session_id: str,
    source_file_id: int,  # noqa: ARG001 — kept for orchestrator call symmetry
    seq_counter: dict[str, int],
    existing_message_ids: frozenset[str] = frozenset(),
    existing_tool_call_ids: frozenset[str] = frozenset(),
) -> Iterator[tuple[str, tuple[Any, ...]]]:
    """Yield `(table_name, row_tuple)` pairs for the given record.

    `seq_counter` is mutated: keys `"messages"` and `"tool_calls"` are
    incremented per emitted row. Caller initializes per session.

    Returns nothing for records that are not persisted (attachments, queue
    operations, last-prompt, system, file-history-snapshot, unknown).

    Row tuples match column order in `INSERT_SQL[table_name]`.

    Foreign-key contract: `existing_message_ids` is the set of `messages.id`
    values that this file will produce, and `existing_tool_call_ids` is the
    set of `tool_calls.id` values it will produce. The mapper sets
    `messages.parent_id` only when the record's `parentUuid` is in
    `existing_message_ids` (otherwise NULL), and emits a `tool_results` row
    only when its `tool_use_id` is in `existing_tool_call_ids`. This avoids
    FOREIGN KEY violations when Claude Code resumes from a previous session
    and references uuids that aren't rows in this file. Callers should build
    the sets with a first parser pass before invoking the mapper. Empty sets
    (the defaults) mean "no parent" and "drop all tool_results"; tests that
    don't care about FK linkage can rely on the defaults.
    """
    if isinstance(record, UserMessage):
        yield from _map_user(
            record,
            session_id=session_id,
            seq_counter=seq_counter,
            existing_message_ids=existing_message_ids,
            existing_tool_call_ids=existing_tool_call_ids,
        )
        return
    if isinstance(record, AssistantMessage):
        yield from _map_assistant(
            record,
            session_id=session_id,
            seq_counter=seq_counter,
            existing_message_ids=existing_message_ids,
        )
        return
