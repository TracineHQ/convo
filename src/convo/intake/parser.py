"""JSONL line and file parser for Claude Code session records.

Decision (mixed-block assistant records):
    The pre-flight survey (`docs/plan/intake-pipeline/record-types-survey.md`)
    confirmed that a single assistant turn frequently emits multiple content
    blocks of mixed types in one record — for example `thinking + tool_use`
    or `thinking + text + tool_use`. The survey counts 25 thinking blocks,
    18 tool_use blocks, and 9 text blocks across only 52 assistant records;
    the only way that adds up is by mixing.

    The parser therefore yields exactly ONE `AssistantMessage` per JSONL line,
    with every block (text / thinking / tool_use, in source order) gathered
    under `.blocks`. Splitting these into separate `messages` and `tool_calls`
    schema rows is the mapper's job (Phase A2). Keeping the line-to-record
    mapping 1:1 lets the orchestrator track file offsets and idempotency
    cleanly and lets the mapper own the schema-shape decision in one place.

    The same rule applies to user records: one `UserMessage` per line, with
    `tool_result` blocks and any text content surfaced under `.blocks` /
    `.text_content`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from convo.intake.records import (
    AssistantMessage,
    Attachment,
    FileHistorySnapshot,
    IntakeRecord,
    LastPrompt,
    MessageBlock,
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
    from collections.abc import Iterator
    from pathlib import Path

_BOM = "﻿"
_ERR_INVALID_JSON = "Invalid JSON on line {lineno}: {reason}"
_ERR_NOT_OBJECT = "Line {lineno} is valid JSON but not an object"


class IntakeParseError(ValueError):
    """Raised when a JSONL line cannot be parsed as a record.

    Carries the offending line content and (where known) its 1-based line
    number so callers can decide whether to skip the line or abort the file.
    """

    def __init__(self, line: str, lineno: int, reason: str) -> None:
        super().__init__(reason)
        self.line = line
        self.lineno = lineno
        self.reason = reason


def _parse_block(block: dict[str, Any]) -> MessageBlock | None:
    """Build a typed message block from a raw dict; return None on unknown type."""
    btype = block.get("type")
    if btype == "text":
        return TextBlock(text=str(block.get("text", "")), raw=block)
    if btype == "thinking":
        sig = block.get("signature")
        return ThinkingBlock(
            thinking=str(block.get("thinking", "")),
            signature=str(sig) if sig is not None else None,
            raw=block,
        )
    if btype == "tool_use":
        raw_input = block.get("input")
        input_obj: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        return ToolUseBlock(
            id=str(block.get("id", "")),
            name=str(block.get("name", "")),
            input=input_obj,
            raw=block,
        )
    if btype == "tool_result":
        content = block.get("content", "")
        if not isinstance(content, str) and not isinstance(content, list):
            content = str(content)
        is_error_raw = block.get("is_error")
        is_error = bool(is_error_raw) if is_error_raw is not None else None
        return ToolResultBlock(
            tool_use_id=str(block.get("tool_use_id", "")),
            content=content,
            is_error=is_error,
            raw=block,
        )
    return None


def _parse_blocks(content: Any) -> tuple[tuple[MessageBlock, ...], str | None]:
    """Normalize a message.content payload into typed blocks plus optional text.

    Returns (blocks, text_content). `text_content` is set when content is a
    plain string (user records carry natural-language input that way).
    """
    if isinstance(content, str):
        return ((), content)
    if isinstance(content, list):
        blocks: list[MessageBlock] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            parsed = _parse_block(item)
            if parsed is not None:
                blocks.append(parsed)
        return (tuple(blocks), None)
    return ((), None)


def _parse_user(raw: dict[str, Any]) -> UserMessage:
    message = raw.get("message")
    content: Any = message.get("content") if isinstance(message, dict) else None
    blocks, text_content = _parse_blocks(content)
    parent = raw.get("parentUuid")
    return UserMessage(
        uuid=str(raw.get("uuid", "")),
        parent_uuid=str(parent) if parent is not None else None,
        session_id=str(raw.get("sessionId", "")),
        timestamp=str(raw.get("timestamp", "")),
        blocks=blocks,
        text_content=text_content,
        raw=raw,
    )


def _parse_assistant(raw: dict[str, Any]) -> AssistantMessage:
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    assert isinstance(message, dict)
    content: Any = message.get("content")
    blocks, _ = _parse_blocks(content)
    parent = raw.get("parentUuid")
    request_id = raw.get("requestId")
    msg_id = message.get("id")
    model = message.get("model")
    return AssistantMessage(
        uuid=str(raw.get("uuid", "")),
        parent_uuid=str(parent) if parent is not None else None,
        session_id=str(raw.get("sessionId", "")),
        timestamp=str(raw.get("timestamp", "")),
        model=str(model) if model is not None else None,
        request_id=str(request_id) if request_id is not None else None,
        message_id=str(msg_id) if msg_id is not None else None,
        blocks=blocks,
        raw=raw,
    )


def _parse_attachment(raw: dict[str, Any]) -> Attachment:
    inner = raw.get("attachment")
    payload: dict[str, Any] = inner if isinstance(inner, dict) else {}
    parent = raw.get("parentUuid")
    return Attachment(
        uuid=str(raw.get("uuid", "")),
        parent_uuid=str(parent) if parent is not None else None,
        session_id=str(raw.get("sessionId", "")),
        timestamp=str(raw.get("timestamp", "")),
        subtype=str(payload.get("type", "")),
        payload=payload,
        raw=raw,
    )


def _parse_queue_operation(raw: dict[str, Any]) -> QueueOperation:
    content = raw.get("content")
    return QueueOperation(
        session_id=str(raw.get("sessionId", "")),
        timestamp=str(raw.get("timestamp", "")),
        operation=str(raw.get("operation", "")),
        content=str(content) if content is not None else None,
        raw=raw,
    )


def _parse_last_prompt(raw: dict[str, Any]) -> LastPrompt:
    return LastPrompt(
        session_id=str(raw.get("sessionId", "")),
        last_prompt=str(raw.get("lastPrompt", "")),
        raw=raw,
    )


def _parse_system(raw: dict[str, Any]) -> SystemRecord:
    parent = raw.get("parentUuid")
    content = raw.get("content")
    return SystemRecord(
        uuid=str(raw.get("uuid", "")),
        parent_uuid=str(parent) if parent is not None else None,
        session_id=str(raw.get("sessionId", "")),
        timestamp=str(raw.get("timestamp", "")),
        subtype=str(raw.get("subtype", "")),
        content=str(content) if content is not None else None,
        raw=raw,
    )


def _parse_file_history(raw: dict[str, Any]) -> FileHistorySnapshot:
    snap = raw.get("snapshot")
    return FileHistorySnapshot(
        message_id=str(raw.get("messageId", "")),
        snapshot=snap if isinstance(snap, dict) else {},
        is_snapshot_update=bool(raw.get("isSnapshotUpdate", False)),
        raw=raw,
    )


_DISPATCH: dict[str, object] = {
    "user": _parse_user,
    "assistant": _parse_assistant,
    "attachment": _parse_attachment,
    "queue-operation": _parse_queue_operation,
    "last-prompt": _parse_last_prompt,
    "system": _parse_system,
    "file-history-snapshot": _parse_file_history,
}


def parse_line(raw: str, lineno: int = 0) -> IntakeRecord:
    """Parse a single JSONL line into an `IntakeRecord`.

    Raises `IntakeParseError` on invalid JSON or non-object payloads. Unknown
    top-level `type` values are returned as `UnknownRecord` so the orchestrator
    can count and continue.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntakeParseError(
            line=raw,
            lineno=lineno,
            reason=_ERR_INVALID_JSON.format(lineno=lineno, reason=exc.msg),
        ) from exc
    if not isinstance(obj, dict):
        raise IntakeParseError(
            line=raw,
            lineno=lineno,
            reason=_ERR_NOT_OBJECT.format(lineno=lineno),
        )
    rec_type = obj.get("type")
    if isinstance(rec_type, str):
        handler = _DISPATCH.get(rec_type)
        if handler is not None:
            fn: Any = handler
            result: IntakeRecord = fn(obj)
            return result
        return UnknownRecord(type_=rec_type, raw=obj)
    return UnknownRecord(type_=str(rec_type), raw=obj)


def parse_file(path: Path) -> Iterator[IntakeRecord]:
    """Yield `IntakeRecord` for each non-blank line in `path`.

    Reads the file lazily in binary mode, decodes UTF-8, strips a leading BOM
    on the first line, skips blank lines, and tolerates trailing newlines. On
    a malformed line, raises `IntakeParseError` with the 1-based line number;
    the caller decides whether to skip or abort.

    Decode policy: strict UTF-8. A malformed byte sequence raises
    `UnicodeDecodeError` (no `errors="replace"`) — fail fast on a corrupt
    file rather than silently rewriting bytes. The orchestrator catches this
    at `index_file` and converts it into a per-file `IndexResult.error` so
    one bad file doesn't abort the whole tree run.
    """
    with path.open("rb") as fh:
        for lineno, raw_bytes in enumerate(fh, start=1):
            text = raw_bytes.decode("utf-8")
            if lineno == 1 and text.startswith(_BOM):
                text = text[len(_BOM) :]
            stripped = text.strip()
            if not stripped:
                continue
            yield parse_line(stripped, lineno=lineno)
