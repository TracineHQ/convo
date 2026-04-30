"""Port a legacy convo DB (TracineHQ/claude-skills) to the new schema.

Boundary: this module does NOT honor the global `--db` flag. The
`migrate-legacy` subcommand owns its own `--src` / `--dest` paths so that
the same env precedence (`CONVO_DB`) can be reused for both, and the
default canonical case (`~/.claude/convo.db`) flows naturally through
`_resolve_paths`.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    import sqlite3
    from collections.abc import Iterator


_CONVO_LEGACY_NS: uuid.UUID = uuid.uuid5(
    uuid.NAMESPACE_DNS,
    "convo-legacy.tracinehq.github",
)

_DEFAULT_DB_PATH: Path = Path.home() / ".claude" / "convo.db"

_ERR_SRC_NOT_LEGACY = (
    "--src does not look like a legacy convo DB "
    "(expected `conversations` table, no `schema_migrations` table)"
)
_ERR_SAME_PATH_NO_KEEP = (
    "--src and --dest resolve to the same path; pass --keep-legacy "
    "(default) or specify a different --dest"
)
_ERR_RENAMED_EXISTS = (
    "refusing to auto-rename: {path} already exists. Remove it (or "
    "pass --src/--dest explicitly) and rerun."
)

_RESUME_DEFERRED_MSG = (
    "deferred tables not yet supported (waiting on convo v0.2 / 0002_live_hooks.sql)"
)


def _resolve_one(explicit: Path | None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve(strict=False)
    env = os.environ.get("CONVO_DB")
    if env:
        return Path(env).expanduser().resolve(strict=False)
    return _DEFAULT_DB_PATH.expanduser().resolve(strict=False)


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, bool]:
    """Return (src_path, dest_path, same_path) after env+default+resolve."""
    src = _resolve_one(args.src)
    dest = _resolve_one(args.dest)
    return src, dest, src == dest


def _handle_resume_deferred(args: argparse.Namespace) -> int:
    # Phase 03 fills in the marker validation; Phase 01 prints the canonical
    # waiting message and exits 0.
    del args
    print(_RESUME_DEFERRED_MSG)
    return 0


def run(args: argparse.Namespace) -> int:
    if args.resume_deferred:
        return _handle_resume_deferred(args)

    src_path, dest_path, same_path = _resolve_paths(args)

    if same_path:
        if args.no_keep_legacy and not args.dry_run:
            print(_ERR_SAME_PATH_NO_KEEP, file=sys.stderr)
            return 1
        if not args.dry_run:
            renamed = src_path.with_name("convo-legacy.db")
            if renamed.exists():
                print(
                    _ERR_RENAMED_EXISTS.format(path=renamed),
                    file=sys.stderr,
                )
                return 1
            src_path.rename(renamed)
            print(f"renamed {src_path} -> {renamed}", file=sys.stderr)
            src_path = renamed

    # Phase 02 onward: validate, transform, write dest, etc.
    del dest_path
    return 0


# ---------------------------------------------------------------------------
# Phase 02: per-table transforms
# ---------------------------------------------------------------------------

_VALID_ROLES = frozenset({"user", "assistant", "system"})

_TOOL_RESULTS_NOOP_NOTE = (
    "tool_results: 0 -> 0 (legacy never captured this; intake plan will populate)"
)


def _synth_tool_call_id(conversation_id: str, legacy_id: int) -> str:
    """Stable, deterministic synthetic tool_call id for legacy rows."""
    return str(uuid.uuid5(_CONVO_LEGACY_NS, f"{conversation_id}:{legacy_id}"))


def _resolve_message_id_for_tool_call(
    src: sqlite3.Connection,
    conversation_id: str,
    timestamp: str | None,
) -> int | None:
    """Return the lowest-id assistant message for `(conversation_id, timestamp)`.

    Returns None if `timestamp` is None or no matching assistant message exists.
    """
    if timestamp is None:
        return None
    row = src.execute(
        "SELECT id FROM messages "
        "WHERE conversation_id = ? AND timestamp = ? AND role = 'assistant' "
        "ORDER BY id ASC LIMIT 1",
        (conversation_id, timestamp),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def migrate_source_files(
    src: sqlite3.Connection,
) -> Iterator[tuple[Any, ...]]:
    """Yield (path, kind, sha256, size, mtime_ns, last_indexed_at, message_count).

    Insert order matches the new `source_files` table without `id` (it is
    auto-assigned).
    """
    cur = src.execute(
        "SELECT path, mtime_ns, size_bytes, indexed_at FROM indexed_files",
    )
    for row in cur:
        yield (
            row["path"],
            "transcript",
            None,  # sha256
            int(row["size_bytes"]),
            int(row["mtime_ns"]),
            row["indexed_at"],
            0,  # message_count populated by intake plan
        )


def migrate_sessions(
    src: sqlite3.Connection,
    path_to_id: dict[str, int],
) -> Iterator[tuple[tuple[Any, ...], int]]:
    """Yield ((session_row, ...), drop_delta).

    drop_delta is 1 when the conversation's `path` cannot be resolved against
    the new-side `source_files` map (orphan), 0 otherwise.
    """
    cur = src.execute(
        "SELECT id, path, cwd, started_at, ended_at, model, git_branch FROM conversations",
    )
    for row in cur:
        new_id = path_to_id.get(row["path"])
        if new_id is None:
            yield (), 1
            continue
        yield (
            (
                row["id"],
                new_id,
                row["cwd"],
                row["started_at"],
                row["ended_at"],
                row["model"],
                row["git_branch"],
                None,  # git_commit; legacy did not capture
            ),
            0,
        )


def migrate_messages(
    src: sqlite3.Connection,
) -> Iterator[tuple[tuple[Any, ...], int]]:
    """Yield ((message_row, ...), drop_delta) with synthesized raw_json."""
    cur = src.execute(
        "SELECT id, conversation_id, timestamp, role, content_length, sequence_num FROM messages",
    )
    for row in cur:
        role = row["role"]
        if role not in _VALID_ROLES:
            yield (), 1
            continue
        raw_json = json.dumps(
            {
                "role": role,
                "_synthesized": True,
                "_legacy_id": int(row["id"]),
                "_legacy_content_length": (
                    int(row["content_length"]) if row["content_length"] is not None else None
                ),
            },
            separators=(",", ":"),
        )
        yield (
            (
                f"legacy:{int(row['id'])}",
                row["conversation_id"],
                None,  # parent_id
                role,
                int(row["sequence_num"]) if row["sequence_num"] is not None else 0,
                row["timestamp"],
                "",  # content; intake plan re-populates
                0,  # has_newlines
                raw_json,
            ),
            0,
        )


def migrate_tool_calls(
    src: sqlite3.Connection,
) -> Iterator[tuple[tuple[Any, ...], int]]:
    """Yield ((tool_call_row, ...), drop_delta).

    Drops rows whose `(conversation_id, timestamp)` cannot be resolved to an
    assistant message in the source.
    """
    cur = src.execute(
        "SELECT id, conversation_id, timestamp, tool_name, input_json, "
        "has_newlines, sequence_num FROM tool_calls",
    )
    for row in cur:
        resolved = _resolve_message_id_for_tool_call(
            src,
            row["conversation_id"],
            row["timestamp"],
        )
        if resolved is None:
            yield (), 1
            continue
        input_json = row["input_json"] if row["input_json"] is not None else "{}"
        yield (
            (
                _synth_tool_call_id(row["conversation_id"], int(row["id"])),
                f"legacy:{resolved}",
                row["conversation_id"],
                int(row["sequence_num"]) if row["sequence_num"] is not None else 0,
                row["tool_name"],
                input_json,
                None,  # started_at — legacy did not capture
                None,  # ended_at
                None,  # duration_ms
                int(row["has_newlines"]) if row["has_newlines"] is not None else 0,
            ),
            0,
        )
