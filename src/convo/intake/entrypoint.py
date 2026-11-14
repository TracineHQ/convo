"""How a session was started: the `entrypoint` field on Claude Code transcript lines.

`cli` is an interactive session. Programmatic runs carry an `sdk-` prefix:
`sdk-cli` (headless `claude -p`), `sdk-py` (Python Agent SDK) and `sdk-ts`
(TypeScript Agent SDK). `convo tag-sdk` writes `sdk-inferred` for sessions it
tags by heuristic, so the same prefix test covers them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

SDK_ENTRYPOINT_PREFIX: str = "sdk-"
INFERRED_SDK_ENTRYPOINT: str = "sdk-inferred"


def session_entrypoint(raws: Iterable[object]) -> str | None:
    """Return the first non-empty `entrypoint` string among transcript lines.

    Claude Code writes it on every user, assistant and attachment line. Lines
    without one (summaries, snapshots, older Claude Code versions) are skipped.
    """
    for raw in raws:
        if isinstance(raw, dict):
            value = raw.get("entrypoint")
            if isinstance(value, str) and value:
                return value
    return None


def _parsed_lines(path: Path) -> Iterable[object]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def file_entrypoint(path: Path) -> str | None:
    """Read `path` (a transcript JSONL) only as far as its first entrypoint."""
    return session_entrypoint(_parsed_lines(path))
