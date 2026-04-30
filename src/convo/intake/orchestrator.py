"""File-level and tree-level intake orchestrators."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from convo.intake.mapper import INSERT_SQL, map_record
from convo.intake.parser import IntakeParseError, parse_file
from convo.intake.records import AssistantMessage, ToolUseBlock, UnknownRecord, UserMessage
from convo.intake.signature import compute_file_signature

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from convo.db import Database
    from convo.intake.records import IntakeRecord

_ERR_DB_NOT_OPEN = "Database is not open"

_SKIP_UNCHANGED = "unchanged"
_SKIP_EMPTY = "empty"
_SKIP_DRY_RUN_NEW = "dry_run_new"
_SKIP_DRY_RUN_UNCHANGED = "dry_run_unchanged"
_SKIP_DRY_RUN_FORCE = "dry_run_force_reindex"

_INSERT_SOURCE_FILE = (
    "INSERT INTO source_files(path, kind, size, mtime_ns, sha256, last_indexed_at, message_count) "
    "VALUES (?, 'transcript', ?, ?, ?, ?, 0)"
)
_INSERT_SESSION = (
    "INSERT OR IGNORE INTO sessions("
    "id, source_file_id, project_path, started_at, ended_at, model, git_branch, git_commit"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_UPDATE_SOURCE_FILE = "UPDATE source_files SET last_indexed_at = ?, message_count = ? WHERE id = ?"


@dataclass(frozen=True, slots=True)
class IndexResult:
    """Outcome of indexing one source file."""

    path: Path
    source_file_id: int | None
    inserted_rows: dict[str, int] = field(default_factory=dict)
    skipped_reason: str | None = None
    error: str | None = None
    error_at_line: int | None = None
    unknown_types: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IndexReport:
    """Aggregate outcome of indexing a tree of source files."""

    files_seen: int
    files_indexed: int
    files_skipped_unchanged: int
    files_skipped_empty: int
    files_failed: int
    rows_inserted: dict[str, int]
    unknown_record_types: dict[str, int]
    errors: list[tuple[Path, str, int | None]]
    duration_ms: int


@dataclass(frozen=True, slots=True)
class _FileSig:
    path_str: str
    size: int
    mtime_ns: int
    sha_hex: str
    session_id: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _session_id_from_path(path: Path) -> str:
    return path.stem


def _lookup_existing(
    conn: Any,
    path_str: str,
) -> tuple[int, str | None] | None:
    row = conn.execute(
        "SELECT id, sha256 FROM source_files WHERE path = ?",
        (path_str,),
    ).fetchone()
    if row is None:
        return None
    return (int(row["id"]), row["sha256"])


def _delete_existing(conn: Any, source_file_id: int) -> None:
    conn.execute("DELETE FROM source_files WHERE id = ?", (source_file_id,))


def _is_empty_file(path: Path) -> bool:
    if path.stat().st_size == 0:
        return True
    with path.open("rb") as fh:
        for raw in fh:
            if raw.strip():
                return False
    return True


def _session_metadata(
    records: list[IntakeRecord],
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    started_at: str | None = None
    ended_at: str | None = None
    model: str | None = None
    project_path: str | None = None
    git_branch: str | None = None
    for rec in records:
        ts = getattr(rec, "timestamp", None)
        if isinstance(ts, str) and ts:
            if started_at is None:
                started_at = ts
            ended_at = ts
        raw = getattr(rec, "raw", None)
        if isinstance(raw, dict):
            cwd = raw.get("cwd")
            if project_path is None and isinstance(cwd, str) and cwd:
                project_path = cwd
            branch = raw.get("gitBranch")
            if git_branch is None and isinstance(branch, str) and branch:
                git_branch = branch
        if model is None and isinstance(rec, AssistantMessage) and rec.model:
            model = rec.model
    return (started_at, ended_at, model, project_path, git_branch)


def _collect_in_file_ids(
    records: list[IntakeRecord],
) -> tuple[frozenset[str], frozenset[str]]:
    """First-pass scan: collect uuids that will become `messages` / `tool_calls` rows.

    Used by the mapper to suppress FK-violating cross-file references — see
    `map_record` docstring for the contract. Only `UserMessage` and
    `AssistantMessage` produce rows; their `uuid` (when non-empty) becomes the
    `messages.id`. `ToolUseBlock.id` (when non-empty) becomes a `tool_calls.id`.
    Empty uuids are skipped here because the mapper will synth a uuid5 at
    map-time, and a synthesized id by definition can't be referenced by a
    `parentUuid` field.
    """
    message_ids: set[str] = set()
    tool_call_ids: set[str] = set()
    for rec in records:
        if isinstance(rec, (UserMessage, AssistantMessage)) and rec.uuid:
            message_ids.add(rec.uuid)
        if isinstance(rec, AssistantMessage):
            for block in rec.blocks:
                if isinstance(block, ToolUseBlock) and block.id:
                    tool_call_ids.add(block.id)
    return frozenset(message_ids), frozenset(tool_call_ids)


def _build_batches(
    records: list[IntakeRecord],
    *,
    session_id: str,
    source_file_id: int,
) -> dict[str, list[tuple[Any, ...]]]:
    batches: dict[str, list[tuple[Any, ...]]] = {
        "messages": [],
        "tool_calls": [],
        "tool_results": [],
    }
    seq_counter: dict[str, int] = {"messages": 0, "tool_calls": 0}
    existing_message_ids, existing_tool_call_ids = _collect_in_file_ids(records)
    for rec in records:
        if not isinstance(rec, (UserMessage, AssistantMessage)):
            continue
        for table, row in map_record(
            rec,
            session_id=session_id,
            source_file_id=source_file_id,
            seq_counter=seq_counter,
            existing_message_ids=existing_message_ids,
            existing_tool_call_ids=existing_tool_call_ids,
        ):
            batches[table].append(row)
    return batches


def _persist(
    conn: Any,
    sig: _FileSig,
    records: list[IntakeRecord],
) -> tuple[int, dict[str, int]]:
    now_iso = _now_iso()
    cur = conn.execute(
        _INSERT_SOURCE_FILE,
        (sig.path_str, sig.size, sig.mtime_ns, sig.sha_hex, now_iso),
    )
    source_file_id = int(cur.lastrowid) if cur.lastrowid is not None else 0

    started_at, ended_at, model, project_path, git_branch = _session_metadata(records)
    conn.execute(
        _INSERT_SESSION,
        (
            sig.session_id,
            source_file_id,
            project_path,
            started_at,
            ended_at,
            model,
            git_branch,
            None,
        ),
    )

    batches = _build_batches(records, session_id=sig.session_id, source_file_id=source_file_id)
    for table, rows in batches.items():
        if rows:
            conn.executemany(INSERT_SQL[table], rows)

    conn.execute(_UPDATE_SOURCE_FILE, (now_iso, len(batches["messages"]), source_file_id))

    return source_file_id, {table: len(rows) for table, rows in batches.items()}


def _count_unknown_types(records: list[IntakeRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rec in records:
        if isinstance(rec, UnknownRecord):
            counts[rec.type_] = counts.get(rec.type_, 0) + 1
    return counts


def index_file(db: Database, path: Path, *, force: bool = False) -> IndexResult:
    """Index `path` into `db`. Returns an `IndexResult` describing the outcome.

    Idempotent on repeat invocations: a second call against an unchanged file
    returns `skipped_reason="unchanged"` without writing. With `force=True`,
    the prior `source_files` row is deleted (cascading to messages, tool_calls,
    tool_results) and the file is re-indexed in a single transaction. On any
    `IntakeParseError` mid-file the transaction rolls back and the DB is
    untouched.
    """
    if db.conn is None:
        raise RuntimeError(_ERR_DB_NOT_OPEN)
    conn = db.conn
    path_str = str(path)

    if _is_empty_file(path):
        return IndexResult(path=path, source_file_id=None, skipped_reason=_SKIP_EMPTY)

    sha256, size, mtime_ns = compute_file_signature(path)
    sig = _FileSig(
        path_str=path_str,
        size=size,
        mtime_ns=mtime_ns,
        sha_hex=sha256.hex(),
        session_id=_session_id_from_path(path),
    )

    existing = _lookup_existing(conn, path_str)
    if existing is not None and not force and existing[1] == sig.sha_hex:
        return IndexResult(
            path=path,
            source_file_id=existing[0],
            skipped_reason=_SKIP_UNCHANGED,
        )

    conn.commit()
    conn.execute("BEGIN EXCLUSIVE")
    # Defer FK checks for the duration of this file's transaction. Claude Code
    # JSONLs are not topologically sorted: a record's `parentUuid` can refer
    # to another record that appears LATER in the file. The mapper's prescan
    # confirms the parent will exist in this file, but per-row FK enforcement
    # rejects the row before its parent has been inserted. With
    # `defer_foreign_keys = 1`, FK checks fire only at COMMIT time — by which
    # point all rows are present.
    conn.execute("PRAGMA defer_foreign_keys = 1")
    try:
        if existing is not None:
            _delete_existing(conn, existing[0])
        try:
            records = list(parse_file(path))
        except IntakeParseError as exc:
            conn.rollback()
            return IndexResult(
                path=path,
                source_file_id=None,
                error=exc.reason,
                error_at_line=exc.lineno,
            )
        try:
            source_file_id, counts = _persist(conn, sig, records)
            conn.commit()
        except sqlite3.DatabaseError as exc:
            # Containment: a per-file DB error (FK / UNIQUE / etc.) must not
            # abort the whole tree run. Roll back this file and report it as a
            # failure on the IndexResult; the orchestrator's _accumulate will
            # collect it into IndexReport.errors.
            conn.rollback()
            return IndexResult(
                path=path,
                source_file_id=None,
                error=str(exc),
                error_at_line=None,
            )
    except BaseException:
        conn.rollback()
        raise

    return IndexResult(
        path=path,
        source_file_id=source_file_id,
        inserted_rows=counts,
        unknown_types=_count_unknown_types(records),
    )


def _classify_dry_run(
    conn: Any,
    path: Path,
    *,
    full: bool,
) -> IndexResult:
    if _is_empty_file(path):
        return IndexResult(path=path, source_file_id=None, skipped_reason=_SKIP_EMPTY)
    sha256, _size, _mtime = compute_file_signature(path)
    sha_hex = sha256.hex()
    existing = _lookup_existing(conn, str(path))
    if existing is None:
        return IndexResult(path=path, source_file_id=None, skipped_reason=_SKIP_DRY_RUN_NEW)
    if full:
        return IndexResult(
            path=path,
            source_file_id=existing[0],
            skipped_reason=_SKIP_DRY_RUN_FORCE,
        )
    if existing[1] == sha_hex:
        return IndexResult(
            path=path,
            source_file_id=existing[0],
            skipped_reason=_SKIP_DRY_RUN_UNCHANGED,
        )
    return IndexResult(path=path, source_file_id=existing[0], skipped_reason=_SKIP_DRY_RUN_NEW)


def _discover_jsonl(projects_dir: Path) -> list[Path]:
    return sorted(projects_dir.glob("*/*.jsonl"))


@dataclass(slots=True)
class _TreeAccumulator:
    rows_inserted: dict[str, int] = field(
        default_factory=lambda: {"messages": 0, "tool_calls": 0, "tool_results": 0},
    )
    unknown_types: dict[str, int] = field(default_factory=dict)
    errors: list[tuple[Path, str, int | None]] = field(default_factory=list)
    indexed: int = 0
    skipped_unchanged: int = 0
    skipped_empty: int = 0
    failed: int = 0


_INDEXED_REASONS = frozenset({_SKIP_DRY_RUN_NEW, _SKIP_DRY_RUN_FORCE})
_UNCHANGED_REASONS = frozenset({_SKIP_UNCHANGED, _SKIP_DRY_RUN_UNCHANGED})


def _accumulate(acc: _TreeAccumulator, result: IndexResult) -> None:
    if result.error is not None:
        acc.failed += 1
        acc.errors.append((result.path, result.error, result.error_at_line))
    elif result.skipped_reason in _UNCHANGED_REASONS:
        acc.skipped_unchanged += 1
    elif result.skipped_reason == _SKIP_EMPTY:
        acc.skipped_empty += 1
    elif result.skipped_reason in _INDEXED_REASONS:
        acc.indexed += 1
    else:
        acc.indexed += 1
        for table, count in result.inserted_rows.items():
            acc.rows_inserted[table] = acc.rows_inserted.get(table, 0) + count

    for type_name, count in result.unknown_types.items():
        acc.unknown_types[type_name] = acc.unknown_types.get(type_name, 0) + count


def index_tree(
    db: Database,
    projects_dir: Path,
    *,
    full: bool = False,
    dry_run: bool = False,
    on_file: Callable[[IndexResult], None] | None = None,
) -> IndexReport:
    """Walk `<projects_dir>/<slug>/*.jsonl` and index each file.

    With `full=True`, every file is force-reindexed regardless of sha256.
    With `dry_run=True`, the tree is classified (`new` / `unchanged` /
    `force_reindex` / `empty`) without writing.

    `on_file` receives each per-file `IndexResult` immediately for progress
    reporting.
    """
    if db.conn is None:
        raise RuntimeError(_ERR_DB_NOT_OPEN)

    files = _discover_jsonl(projects_dir)
    acc = _TreeAccumulator()

    start_ns = time.monotonic_ns()
    for path in files:
        if dry_run:
            result = _classify_dry_run(db.conn, path, full=full)
        else:
            result = index_file(db, path, force=full)
        _accumulate(acc, result)
        if on_file is not None:
            on_file(result)

    duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000

    return IndexReport(
        files_seen=len(files),
        files_indexed=acc.indexed,
        files_skipped_unchanged=acc.skipped_unchanged,
        files_skipped_empty=acc.skipped_empty,
        files_failed=acc.failed,
        rows_inserted=acc.rows_inserted,
        unknown_record_types=acc.unknown_types,
        errors=acc.errors,
        duration_ms=int(duration_ms),
    )
