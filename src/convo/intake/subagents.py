"""Subagent transcripts: where Claude Code writes them and how they become sessions.

A subagent's transcript sits beside its parent session, not in the project
directory itself:

    <projects>/<slug>/<parent-session-id>/subagents/agent-<id>.jsonl
    <projects>/<slug>/<parent-session-id>/subagents/workflows/<wf>/agent-<id>.jsonl

with an `agent-<id>.meta.json` next to each (agent type, description, ...).

Each one is indexed as a session of its own, with no schema change. Its id is
`<id>.<parent-session-id>`: the agent id Claude Code reports (`agentId`), a
dot, then the parent session's id, so the link is readable from the `sessions`
table alone. The parent id comes last so that a prefix of a top-level session
id (`convo inspect <prefix>`) never also matches its subagents, and the agent
id comes first so the 8-character form `search` prints still tells subagents
apart. Neither half contains a dot, and no top-level session id does, so a dot
marks a subagent session.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

SUBAGENT_DIR = "subagents"
_AGENT_PREFIX = "agent-"

# `**` spans the optional `workflows/<wf>/` levels. The name filter leaves out
# the workflow `journal.jsonl` files, which are not transcripts.
ALL_SUBAGENTS_GLOB = f"*/*/{SUBAGENT_DIR}/**/{_AGENT_PREFIX}*.jsonl"

# SQL LIKE pattern matching the ids `subagent_session_id` builds.
SUBAGENT_SESSION_LIKE = "%.%"


def subagents_glob(parent_session_id: str) -> str:
    """Glob, relative to the projects dir, for one parent session's subagent transcripts."""
    return f"*/{parent_session_id}/{SUBAGENT_DIR}/**/{_AGENT_PREFIX}*.jsonl"


def subagent_parent_id(path: Path) -> str | None:
    """Return the parent session id if `path` is a subagent transcript, else None."""
    if not path.name.startswith(_AGENT_PREFIX):
        return None
    for ancestor in path.parents:
        if ancestor.name == SUBAGENT_DIR:
            return ancestor.parent.name or None
    return None


def subagent_session_id(path: Path) -> str | None:
    """Return the session id for the subagent transcript at `path`, else None."""
    parent = subagent_parent_id(path)
    if parent is None:
        return None
    return f"{path.stem.removeprefix(_AGENT_PREFIX)}.{parent}"


def read_meta(path: Path) -> dict[str, Any] | None:
    """Read the `agent-<id>.meta.json` beside the transcript at `path`.

    Returns None when it is missing, unreadable or not a JSON object: older
    Claude Code versions wrote no meta file, and a bad one must not fail the
    transcript's indexing.
    """
    try:
        data = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
