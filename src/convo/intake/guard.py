"""Ingest guard's JSONL decision log into convo's SQLite store.

Guard's writer contract is documented in `guard/docs/JSONL_FORMAT.md`. Schema
v1 fields are flat — no mixed content blocks — so this module is much smaller
than `orchestrator.py`. One JSONL line maps to one `guard_decisions` row.

Path discovery follows JSONL_FORMAT.md §3.2:
1. explicit override (CLI flag)
2. `GUARD_DECISIONS_PATH` env var
3. `~/.claude/guard-decisions.jsonl`
4. single-line `{"redirect": "..."}` pointer at the default path (one hop max)
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from convo.intake.orchestrator import (
    _SKIP_EMPTY,
    _SKIP_UNCHANGED,
    IndexResult,
)
from convo.intake.signature import compute_file_signature

if TYPE_CHECKING:
    from collections.abc import Iterator

    from convo.db import Database

_DEFAULT_GUARD_LOG = Path.home() / ".claude" / "guard-decisions.jsonl"
_SUPPORTED_SCHEMA_VERSION = 1
_KIND = "guard_decisions"
_REQUIRED_FIELDS = (
    "v",
    "schema_version",
    "mode",
    "timestamp",
    "hook_id",
    "event",
    "decision",
    "reason",
    "session_id",
)
_VALID_DECISIONS = frozenset({"allow", "deny", "ask", "defer", "pass"})
_VALID_MODES = frozenset({"enforce", "shadow", "off"})

_INSERT_SOURCE_FILE = (
    "INSERT INTO source_files(path, kind, size, mtime_ns, sha256, last_indexed_at, message_count) "
    "VALUES (?, 'guard_decisions', ?, ?, ?, ?, 0)"
)
_INSERT_DECISION = (
    "INSERT INTO guard_decisions("
    "source_file_id, line_no, schema_version, mode, timestamp, hook_id, event, "
    "tool_name, decision, reason, command_excerpt, session_id, cwd, raw_json"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_UPDATE_SOURCE_FILE = "UPDATE source_files SET last_indexed_at = ?, message_count = ? WHERE id = ?"

_ERR_DB_NOT_OPEN = "Database is not open"


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """One parsed guard decision record (schema v1)."""

    line_no: int
    schema_version: int
    mode: str
    timestamp: str
    hook_id: str
    event: str
    tool_name: str | None
    decision: str
    reason: str
    command_excerpt: str | None
    session_id: str
    cwd: str | None
    raw_json: str


@dataclass(frozen=True, slots=True)
class QuarantinedRecord:
    """A line that did not parse as a usable v1 record. Skipped at insert time."""

    line_no: int
    reason: str
    raw: str


def resolve_guard_log_path(explicit: Path | str | None = None) -> Path | None:
    """Return the resolved guard log path, or None if no log exists.

    Precedence: explicit > $GUARD_DECISIONS_PATH > default. If the resolved
    path is a one-line `{"redirect": "..."}` pointer, follow once.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
    else:
        env = os.environ.get("GUARD_DECISIONS_PATH")
        path = Path(env).expanduser() if env else _DEFAULT_GUARD_LOG
    if not path.exists():
        return None
    redirect = _read_redirect_pointer(path)
    if redirect is not None:
        return redirect if redirect.exists() else None
    return path


def _read_redirect_pointer(path: Path) -> Path | None:
    """If `path` is exactly one JSON object with key 'redirect', return its target."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
            extra = fh.readline()
    except OSError:
        return None
    line = first.strip()
    if extra or not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or set(obj.keys()) != {"redirect"}:
        return None
    target = obj["redirect"]
    if not isinstance(target, str):
        return None
    return Path(target).expanduser()


def parse_guard_file(path: Path) -> Iterator[GuardDecision | QuarantinedRecord]:
    """Yield one record per JSONL line. Unparseable / unsupported lines are quarantined."""
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            stripped = raw.rstrip("\n")
            if not stripped.strip():
                continue
            yield _parse_one(lineno, stripped)


def _validation_reject(obj: object) -> str | None:
    """Return a quarantine reason string if `obj` is not a usable v1 record, else None."""
    if not isinstance(obj, dict):
        return "not_object"
    v = obj.get("v")
    if v != _SUPPORTED_SCHEMA_VERSION:
        return f"unsupported_v: {v!r}"
    missing = [f for f in _REQUIRED_FIELDS if f not in obj]
    if missing:
        return f"missing_fields: {missing}"
    if obj["decision"] not in _VALID_DECISIONS:
        return f"unknown_decision: {obj['decision']!r}"
    if obj["mode"] not in _VALID_MODES:
        return f"unknown_mode: {obj['mode']!r}"
    return None


def _parse_one(lineno: int, raw: str) -> GuardDecision | QuarantinedRecord:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return QuarantinedRecord(line_no=lineno, reason=f"invalid_json: {exc}", raw=raw)
    reject_reason = _validation_reject(obj)
    if reject_reason is not None:
        return QuarantinedRecord(line_no=lineno, reason=reject_reason, raw=raw)
    return GuardDecision(
        line_no=lineno,
        schema_version=int(obj["schema_version"]),
        mode=obj["mode"],
        timestamp=obj["timestamp"],
        hook_id=obj["hook_id"],
        event=obj["event"],
        tool_name=obj.get("tool_name"),
        decision=obj["decision"],
        reason=obj["reason"],
        command_excerpt=obj.get("command_excerpt"),
        session_id=obj["session_id"],
        cwd=obj.get("cwd"),
        raw_json=raw,
    )


def index_guard_file(
    db: Database,
    path: Path,
    *,
    force: bool = False,
) -> IndexResult:
    """Index a guard JSONL log file into `db.guard_decisions`.

    Idempotent on unchanged files. With `force=True`, the prior `source_files`
    row is deleted (cascading to guard_decisions) and the file is re-indexed.
    """
    if db.conn is None:
        raise RuntimeError(_ERR_DB_NOT_OPEN)
    conn = db.conn

    if path.stat().st_size == 0:
        return IndexResult(path=path, source_file_id=None, skipped_reason=_SKIP_EMPTY)

    sha256, size, mtime_ns = compute_file_signature(path)
    sha_hex = sha256.hex()
    path_str = str(path)
    existing = conn.execute(
        "SELECT id, sha256 FROM source_files WHERE path = ?",
        (path_str,),
    ).fetchone()
    if existing is not None and not force and existing["sha256"] == sha_hex:
        return IndexResult(
            path=path,
            source_file_id=int(existing["id"]),
            skipped_reason=_SKIP_UNCHANGED,
        )

    conn.commit()
    conn.execute("BEGIN EXCLUSIVE")
    try:
        if existing is not None:
            conn.execute("DELETE FROM source_files WHERE id = ?", (int(existing["id"]),))
        now_iso = datetime.now(UTC).isoformat()
        cur = conn.execute(
            _INSERT_SOURCE_FILE,
            (path_str, size, mtime_ns, sha_hex, now_iso),
        )
        source_file_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
        rows, quarantined = _collect_decision_rows(path, source_file_id)
        if rows:
            conn.executemany(_INSERT_DECISION, rows)
        conn.execute(_UPDATE_SOURCE_FILE, (now_iso, len(rows), source_file_id))
        conn.commit()
    except sqlite3.DatabaseError as exc:
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

    counts = {"guard_decisions": len(rows)}
    if quarantined:
        counts["quarantined"] = quarantined
    return IndexResult(
        path=path,
        source_file_id=source_file_id,
        inserted_rows=counts,
    )


def _collect_decision_rows(
    path: Path,
    source_file_id: int,
) -> tuple[list[tuple[Any, ...]], int]:
    """Parse `path` and return (insert_rows, quarantined_count)."""
    rows: list[tuple[Any, ...]] = []
    quarantined = 0
    for rec in parse_guard_file(path):
        if isinstance(rec, QuarantinedRecord):
            quarantined += 1
            continue
        rows.append(
            (
                source_file_id,
                rec.line_no,
                rec.schema_version,
                rec.mode,
                rec.timestamp,
                rec.hook_id,
                rec.event,
                rec.tool_name,
                rec.decision,
                rec.reason,
                rec.command_excerpt,
                rec.session_id,
                rec.cwd,
                rec.raw_json,
            ),
        )
    return rows, quarantined
