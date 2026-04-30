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
import random
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    import argparse
    import sqlite3
    from collections.abc import Iterator

    from convo.db import Database


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
    del args
    marker_path = _marker_path()
    if marker_path.exists():
        try:
            marker = _read_marker(marker_path)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"marker read failed: {exc}", file=sys.stderr)
            return 1
        src_path = Path(marker["source_path"])
        stale, reason = _is_marker_stale(marker, src_path)
        if stale:
            print(_ERR_MARKER_STALE.format(reason=reason), file=sys.stderr)
            return 1
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


# ---------------------------------------------------------------------------
# Phase 03: validation pass
# ---------------------------------------------------------------------------


_FTS_PROBE_MIN_INPUT_LEN = 8
_FTS_PROBE_SAMPLE_K = 5
_MESSAGES_FTS_SKIP_REASON = (
    "messages_fts probes skipped — all migrated messages have synthesized content"
)

_ERR_VALIDATION_COUNT = (
    "post-migration count mismatch on table {table}: "
    "expected {expected} (legacy {legacy} - dropped {dropped}), got {actual}"
)
_ERR_VALIDATION_SAMPLE = (
    "post-migration content mismatch on {table} rowid {rowid}: "
    "field {field}; legacy={legacy!r} new={new!r}"
)
_ERR_VALIDATION_FTS = "post-migration FTS round-trip miss on {table} for substring {sub!r}"


class ValidationError(Exception):
    """Raised by `validate()` when any contract check fails."""


@dataclass(frozen=True)
class ValidationReport:
    counts_passed: bool
    counts_detail: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    samples_passed: int = 0
    samples_failed: int = 0
    samples_per_table: int = 0
    fts_probes_passed: int = 0
    fts_probes_failed: int = 0
    fts_skipped_reason: str | None = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def _stable_sample_indices(n_total: int, k: int, seed: int) -> list[int]:
    """Reproducible sample of `min(k, n_total)` unique indices in [0, n_total).

    Uses only `random.Random(seed).random()` to remain stable across Python
    versions where `random.sample()` is not contractually stable.
    """
    if n_total <= 0 or k <= 0:
        return []
    rng = random.Random(seed)  # noqa: S311 — stable sampling, not crypto
    seen: set[int] = set()
    out: list[int] = []
    target = min(k, n_total)
    while len(out) < target:
        idx = int(rng.random() * n_total)
        if idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def _fts5_quoted(substring: str) -> str:
    """Wrap a substring as an FTS5 string literal; double any embedded quotes."""
    return '"' + substring.replace('"', '""') + '"'


def _validate_counts(
    legacy: sqlite3.Connection,
    new_db: Database,
    dropped_per_table: dict[str, int],
) -> dict[str, tuple[int, int, int]]:
    if new_db.conn is None:
        msg = "new_db is not open"
        raise ValidationError(msg)
    detail: dict[str, tuple[int, int, int]] = {}
    legacy_to_new = {
        "indexed_files": ("source_files", "indexed_files"),
        "conversations": ("sessions", "conversations"),
        "messages": ("messages", "messages"),
        "tool_calls": ("tool_calls", "tool_calls"),
    }
    for legacy_table, (new_table, _) in legacy_to_new.items():
        legacy_count = legacy.execute(
            f"SELECT count(*) FROM {legacy_table}",  # noqa: S608
        ).fetchone()[0]
        new_count = new_db.conn.execute(
            f"SELECT count(*) FROM {new_table}",  # noqa: S608
        ).fetchone()[0]
        dropped = dropped_per_table.get(new_table, 0)
        expected = legacy_count - dropped
        if new_count != expected:
            raise ValidationError(
                _ERR_VALIDATION_COUNT.format(
                    table=new_table,
                    expected=expected,
                    legacy=legacy_count,
                    dropped=dropped,
                    actual=new_count,
                ),
            )
        detail[new_table] = (legacy_count, new_count, dropped)
    return detail


def _validate_samples(
    legacy: sqlite3.Connection,
    new_db: Database,
    seed: int,
    samples_per_table: int,
) -> tuple[int, int]:
    if new_db.conn is None:
        msg = "new_db is not open"
        raise ValidationError(msg)
    passed = 0
    # source_files / sessions: by path / id round-trip
    legacy_paths = [r["path"] for r in legacy.execute("SELECT path FROM indexed_files")]
    indices = _stable_sample_indices(len(legacy_paths), samples_per_table, seed)
    for i in indices:
        path = legacy_paths[i]
        new_row = new_db.conn.execute(
            "SELECT path FROM source_files WHERE path = ?",
            (path,),
        ).fetchone()
        if new_row is None or new_row[0] != path:
            raise ValidationError(
                _ERR_VALIDATION_SAMPLE.format(
                    table="source_files",
                    rowid=path,
                    field="path",
                    legacy=path,
                    new=new_row,
                ),
            )
        passed += 1

    # tool_calls: input_json equality on resolvable rows
    legacy_tc = list(
        legacy.execute(
            "SELECT id, conversation_id, tool_name, input_json "
            "FROM tool_calls WHERE input_json IS NOT NULL",
        ),
    )
    indices = _stable_sample_indices(len(legacy_tc), samples_per_table, seed)
    for i in indices:
        row = legacy_tc[i]
        synth_id = _synth_tool_call_id(row["conversation_id"], int(row["id"]))
        new_row = new_db.conn.execute(
            "SELECT name, input_json FROM tool_calls WHERE id = ?",
            (synth_id,),
        ).fetchone()
        if new_row is None:
            # Resolution dropped this row; not a sample failure.
            continue
        if new_row[0] != row["tool_name"]:
            raise ValidationError(
                _ERR_VALIDATION_SAMPLE.format(
                    table="tool_calls",
                    rowid=synth_id,
                    field="name",
                    legacy=row["tool_name"],
                    new=new_row[0],
                ),
            )
        if new_row[1] != row["input_json"]:
            raise ValidationError(
                _ERR_VALIDATION_SAMPLE.format(
                    table="tool_calls",
                    rowid=synth_id,
                    field="input_json",
                    legacy=row["input_json"],
                    new=new_row[1],
                ),
            )
        passed += 1
    return passed, 0


def _validate_fts_probes(
    legacy: sqlite3.Connection,
    new_db: Database,
    seed: int,
) -> tuple[int, int, str | None]:
    del legacy
    if new_db.conn is None:
        msg = "new_db is not open"
        raise ValidationError(msg)
    # Probe targets come from the NEW DB; otherwise rows dropped during the
    # migration would surface as false-negative probe misses.
    eligible = [
        r["input_json"]
        for r in new_db.conn.execute(
            "SELECT input_json FROM tool_calls WHERE input_json IS NOT NULL",
        )
        if len(r["input_json"]) >= _FTS_PROBE_MIN_INPUT_LEN
    ]
    if not eligible:
        # No usable probe targets; treat as PASS=0 / FAIL=0 / no skip reason.
        return 0, 0, None
    indices = _stable_sample_indices(len(eligible), _FTS_PROBE_SAMPLE_K, seed)
    passed = 0
    for i in indices:
        text: str = eligible[i]
        # Middle 4 chars
        mid = len(text) // 2
        sub = text[max(0, mid - 2) : mid + 2]
        quoted = _fts5_quoted(sub)
        rows = new_db.conn.execute(
            "SELECT count(*) FROM tool_calls_fts WHERE tool_calls_fts MATCH ?",
            (quoted,),
        ).fetchone()
        if rows[0] == 0:
            raise ValidationError(
                _ERR_VALIDATION_FTS.format(table="tool_calls_fts", sub=sub),
            )
        passed += 1

    # messages_fts probe is conditional: skip if all messages have empty content.
    has_content = new_db.conn.execute(
        "SELECT 1 FROM messages WHERE content IS NOT NULL AND content != '' LIMIT 1",
    ).fetchone()
    if has_content is None:
        return passed, 0, _MESSAGES_FTS_SKIP_REASON
    return passed, 0, None


def validate(
    legacy: sqlite3.Connection,
    new_db: Database,
    *,
    seed: int = 0xC0FFEE,
    dropped_per_table: dict[str, int] | None = None,
    samples_per_table: int = 5,
) -> ValidationReport:
    """Run count + content + FTS validation; raise ValidationError on failure."""
    drops = dropped_per_table or {}
    counts = _validate_counts(legacy, new_db, drops)
    samples_passed, samples_failed = _validate_samples(
        legacy,
        new_db,
        seed,
        samples_per_table,
    )
    fts_passed, fts_failed, fts_skip = _validate_fts_probes(legacy, new_db, seed)
    return ValidationReport(
        counts_passed=True,
        counts_detail=counts,
        samples_passed=samples_passed,
        samples_failed=samples_failed,
        samples_per_table=samples_per_table,
        fts_probes_passed=fts_passed,
        fts_probes_failed=fts_failed,
        fts_skipped_reason=fts_skip,
    )


# ---------------------------------------------------------------------------
# Phase 03: deferred-table marker
# ---------------------------------------------------------------------------

_MARKER_SCHEMA_VERSION = 1
_DEFAULT_MARKER_PATH: Path = Path.home() / ".claude" / "convo-legacy-deferred.json"
_DEFERRED_TABLE_NAMES: tuple[str, ...] = (
    "hook_tool_events",
    "skill_events",
    "file_access",
    "hook_decisions",
    "cli_sessions",
)
_DEFERRED_BLOCKED_BY = "0002_live_hooks.sql"

_ERR_MARKER_SCHEMA = (
    "marker file schema_version {found} not supported (this convo expects schema_version {known})"
)
_ERR_MARKER_STALE = (
    "marker is stale ({reason}); re-run `convo migrate-legacy` from a "
    "current source DB before retrying --resume-deferred"
)


class DeferredTable(TypedDict):
    name: str
    row_count: int
    blocked_by: str


class Marker(TypedDict):
    schema_version: int
    source_path: str
    source_size: int
    source_mtime_ns: int
    migrated_at: str
    deferred_tables: list[DeferredTable]


def _marker_path() -> Path:
    env = os.environ.get("CONVO_LEGACY_MARKER")
    if env:
        return Path(env).expanduser()
    return _DEFAULT_MARKER_PATH


def _write_marker(src_path: Path, deferred: list[DeferredTable]) -> Path:
    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stat = src_path.stat()
    payload: Marker = {
        "schema_version": _MARKER_SCHEMA_VERSION,
        "source_path": str(src_path),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "migrated_at": datetime.now(UTC).isoformat(),
        "deferred_tables": deferred,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _read_marker(path: Path) -> Marker:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != _MARKER_SCHEMA_VERSION:
        msg = _ERR_MARKER_SCHEMA.format(
            found=raw.get("schema_version"),
            known=_MARKER_SCHEMA_VERSION,
        )
        raise ValueError(msg)
    return raw  # type: ignore[no-any-return]


def _is_marker_stale(marker: Marker, current_src_path: Path) -> tuple[bool, str]:
    if not current_src_path.exists():
        return True, f"source path no longer exists at {current_src_path}"
    stat = current_src_path.stat()
    if stat.st_size != marker["source_size"]:
        return (
            True,
            f"size mismatch (was {marker['source_size']}, now {stat.st_size})",
        )
    if stat.st_mtime_ns != marker["source_mtime_ns"]:
        return (
            True,
            f"mtime mismatch (was {marker['source_mtime_ns']}, now {stat.st_mtime_ns})",
        )
    return False, ""


def report_deferred_tables(src: sqlite3.Connection) -> list[DeferredTable]:
    out: list[DeferredTable] = []
    for name in _DEFERRED_TABLE_NAMES:
        row = src.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        if row is None:
            continue
        count_row = src.execute(
            f"SELECT count(*) FROM {name}",  # noqa: S608
        ).fetchone()
        out.append(
            {
                "name": name,
                "row_count": int(count_row[0]),
                "blocked_by": _DEFERRED_BLOCKED_BY,
            },
        )
    return out
