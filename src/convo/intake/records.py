"""Typed record dataclasses yielded by the JSONL line parser.

The `IntakeRecord` union covers every top-level `type` value seen in real Claude
Code session JSONLs (see `docs/plan/intake-pipeline/record-types-survey.md`).
Block dataclasses (`ToolUseBlock`, `ToolResultBlock`, `TextBlock`,
`ThinkingBlock`) live inside `UserMessage.blocks` / `AssistantMessage.blocks`;
they are not part of the top-level union because they are not first-class JSONL
records.

Every variant carries `raw: dict[str, Any]` — the original parsed JSON — so the
mapper can persist unknown / version-skewed fields into `messages.raw_json` and
`tool_calls.input_json` without losing information.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class TextBlock:
    """`text` content block within a message."""

    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    """`thinking` content block (extended-thinking output, opaque signature)."""

    thinking: str
    signature: str | None
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["thinking"] = "thinking"


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """`tool_use` content block within an assistant message."""

    id: str
    name: str
    input: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """`tool_result` content block within a user message.

    `content` may be a string OR a list of API content sub-blocks (the API
    permits `[{type: "text", text: "..."}]`); the parser does not normalize.
    """

    tool_use_id: str
    content: str | list[dict[str, Any]]
    is_error: bool | None
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["tool_result"] = "tool_result"


type MessageBlock = ToolUseBlock | ToolResultBlock | TextBlock | ThinkingBlock


@dataclass(frozen=True, slots=True)
class UserMessage:
    """Top-level `type=user` record. `content` is normalized into `blocks`."""

    uuid: str
    parent_uuid: str | None
    session_id: str
    timestamp: str
    blocks: tuple[MessageBlock, ...]
    text_content: str | None
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["user"] = "user"


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """Top-level `type=assistant` record.

    `blocks` carries every content block in order — text, thinking, and
    tool_use can be mixed in a single record (see survey: assistant turns
    routinely emit thinking + tool_use together).
    """

    uuid: str
    parent_uuid: str | None
    session_id: str
    timestamp: str
    model: str | None
    request_id: str | None
    message_id: str | None
    blocks: tuple[MessageBlock, ...]
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["assistant"] = "assistant"


@dataclass(frozen=True, slots=True)
class Attachment:
    """Top-level `type=attachment` record. Open-ended `attachment.type` subtypes."""

    uuid: str
    parent_uuid: str | None
    session_id: str
    timestamp: str
    subtype: str
    payload: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["attachment"] = "attachment"


@dataclass(frozen=True, slots=True)
class QueueOperation:
    """Top-level `type=queue-operation` record (drop candidate)."""

    session_id: str
    timestamp: str
    operation: str
    content: str | None
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["queue-operation"] = "queue-operation"


@dataclass(frozen=True, slots=True)
class LastPrompt:
    """Top-level `type=last-prompt` record (drop candidate; duplicates last user record)."""

    session_id: str
    last_prompt: str
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["last-prompt"] = "last-prompt"


@dataclass(frozen=True, slots=True)
class SystemRecord:
    """Top-level `type=system` record (drop candidate; harness telemetry)."""

    uuid: str
    parent_uuid: str | None
    session_id: str
    timestamp: str
    subtype: str
    content: str | None
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["system"] = "system"


@dataclass(frozen=True, slots=True)
class FileHistorySnapshot:
    """Top-level `type=file-history-snapshot` record (drop candidate; harness undo state)."""

    message_id: str
    snapshot: dict[str, Any]
    is_snapshot_update: bool
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["file-history-snapshot"] = "file-history-snapshot"


@dataclass(frozen=True, slots=True)
class UnknownRecord:
    """Fallback for records whose top-level `type` is not recognized."""

    type_: str
    raw: dict[str, Any] = field(default_factory=dict)
    kind: Literal["unknown"] = "unknown"


type IntakeRecord = (
    UserMessage
    | AssistantMessage
    | Attachment
    | QueueOperation
    | LastPrompt
    | SystemRecord
    | FileHistorySnapshot
    | UnknownRecord
)
