"""Synthetic Claude Code transcripts for perf tests.

Ids, content and paths are generated here; nothing comes from real sessions.
Records repeat a four-step cycle (user prompt, assistant tool_use, user
tool_result, assistant text), each record parented on the one before it, so
the indexed rows populate both `messages.parent_id` and
`tool_results.message_id` (the child keys the 0005 indexes cover).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

_BASE_TS = datetime(2026, 9, 1, tzinfo=UTC)
_CWD = "/synthetic/perf"


def _uid(session_id: str, n: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"synthetic:{session_id}:{n}"))


def _record(session_id: str, n: int) -> dict[str, Any]:
    ts = (_BASE_TS + timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    tool_id = f"toolu_{_uid(session_id, n - (n % 4) + 1).replace('-', '')[:24]}"
    step = n % 4
    if step == 0:
        rtype = "user"
        message: dict[str, Any] = {"role": "user", "content": f"synthetic prompt {n}"}
    elif step == 1:
        rtype = "assistant"
        message = {
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "id": f"msg_{n}",
            "content": [
                {"type": "text", "text": f"running step {n}"},
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Bash",
                    "input": {"command": f"echo {n}"},
                },
            ],
        }
    elif step == 2:
        rtype = "user"
        message = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"out {n}",
                    "is_error": False,
                },
            ],
        }
    else:
        rtype = "assistant"
        message = {
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "id": f"msg_{n}",
            "content": [{"type": "text", "text": f"step {n} done"}],
        }
    return {
        "type": rtype,
        "message": message,
        "uuid": _uid(session_id, n),
        "parentUuid": _uid(session_id, n - 1) if n > 0 else None,
        "sessionId": session_id,
        "timestamp": ts,
        "cwd": _CWD,
        "version": "1.0.0",
        "gitBranch": "main",
    }


def write_transcript(path: Path, session_id: str, *, start: int, count: int) -> None:
    """Append records `start .. start+count-1` of `session_id` to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(_record(session_id, n)) + "\n" for n in range(start, start + count)]
    with path.open("a", encoding="utf-8") as fh:
        fh.writelines(lines)
