"""Argparse CLI for convo: backup, restore, index, info, search, inspect, snapshots."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from convo.db import Database, resolve_db_path, resolve_snapshot_dir
from convo.intake.orchestrator import IndexReport, IndexResult, index_tree
from convo.read.filters import parse_span
from convo.read.info import InfoReport, ProjectCount, gather_info
from convo.read.inspect import (
    MessageView,
    SessionView,
    ToolCallView,
    inspect_session,
    resolve_session_id,
)
from convo.read.search import SNIPPET_POST, SNIPPET_PRE, SearchHit, search
from convo.read.snapshots import SnapshotInfo, list_snapshots

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import timedelta

DEFAULT_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"

_INFO_ENVELOPE_VERSION: int = 1
_SEARCH_ENVELOPE_VERSION: int = 1
_INSPECT_ENVELOPE_VERSION: int = 1
_INSPECT_PREVIEW_CHARS: int = 200
_INSPECT_TOOL_INPUT_PREVIEW: int = 80
_UNKNOWN_PROJECT_LABEL: str = "(unknown)"
_BYTES_PER_KIB: int = 1024
_SEARCH_DEFAULT_LIMIT: int = 50
_ANSI_BOLD_ON: str = "\x1b[1m"
_ANSI_BOLD_OFF: str = "\x1b[0m"


def _resolve_projects_dir(explicit: Path | str | None = None) -> Path:
    """Resolve projects dir with precedence: explicit > $CLAUDE_PROJECTS_DIR > default."""
    if explicit is not None:
        return Path(explicit).expanduser()
    env = os.environ.get("CLAUDE_PROJECTS_DIR")
    if env:
        return Path(env).expanduser()
    return DEFAULT_PROJECTS_DIR


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="convo",
        epilog=(
            "Environment variables: CONVO_DB (default DB path), "
            "CONVO_BACKUP_DIR (default snapshot directory, "
            "defaults to <CONVO_DB>'s parent / convo-backups), "
            "CLAUDE_PROJECTS_DIR (default projects dir for `convo index`, "
            "defaults to ~/.claude/projects/)."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="DB path (default: $CONVO_DB or ~/.claude/convo.db)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_backup_parser(sub)
    _add_restore_parser(sub)
    _add_index_parser(sub)
    _add_info_parser(sub)
    _add_search_parser(sub)
    _add_inspect_parser(sub)
    _add_snapshots_parser(sub)
    return parser


def _add_backup_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    backup = sub.add_parser("backup", help="Snapshot the convo DB.")
    backup_dest_group = backup.add_mutually_exclusive_group(required=True)
    backup_dest_group.add_argument(
        "dest",
        nargs="?",
        type=Path,
        help="Explicit snapshot destination (file).",
    )
    backup_dest_group.add_argument(
        "--auto",
        action="store_true",
        help="Write a timestamped snapshot to the snapshot directory.",
    )


def _add_restore_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    restore = sub.add_parser(
        "restore",
        help="Restore the convo DB from a snapshot. The snapshot file is preserved.",
    )
    restore_src_group = restore.add_mutually_exclusive_group(required=True)
    restore_src_group.add_argument(
        "src",
        nargs="?",
        type=Path,
        help="Explicit snapshot file to restore from.",
    )
    restore_src_group.add_argument(
        "--latest",
        action="store_true",
        help="Restore from the newest snapshot in the snapshot directory.",
    )


def _add_index_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    index_p = sub.add_parser(
        "index",
        help="Index Claude Code session JSONLs into the convo DB.",
        epilog=(
            "Walks <projects-dir>/<slug>/*.jsonl. Skips files whose sha256 is "
            "already recorded in source_files. With --full, every file is "
            "re-indexed. CLAUDE_PROJECTS_DIR overrides the default projects dir."
        ),
    )
    index_p.add_argument(
        "--projects-dir",
        type=Path,
        default=None,
        help="Projects dir (default: $CLAUDE_PROJECTS_DIR or ~/.claude/projects/).",
    )
    index_p.add_argument(
        "--full",
        action="store_true",
        help="Re-index every file regardless of recorded sha256.",
    )
    index_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report what would change without writing.",
    )
    index_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _add_info_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    info_p = sub.add_parser(
        "info",
        help="Print an overview of the convo DB (row counts, last index time, snapshots).",
    )
    info_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _add_search_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    search_p = sub.add_parser(
        "search",
        help="Search messages, tool calls, and tool results via FTS5.",
        epilog=(
            "Query is treated as a phrase by default. Prefix tokens with `+` "
            "to require or `-` to exclude (FTS5 NOT). Time spans for --since "
            "use the shorthand <N><unit>: 7d, 24h, 90m, 30s."
        ),
    )
    search_p.add_argument("query", help="Search query string.")
    search_p.add_argument(
        "--since",
        type=parse_span,
        default=None,
        help="Only include hits newer than this span (e.g. 7d, 24h, 90m, 30s).",
    )
    search_p.add_argument(
        "--project",
        default=None,
        help="Restrict to one project_path (exact match against sessions.project_path).",
    )
    search_p.add_argument(
        "--tool",
        default=None,
        help="Restrict tool_call/tool_result hits to this tool name (exact match).",
    )
    search_p.add_argument(
        "--limit",
        type=int,
        default=_SEARCH_DEFAULT_LIMIT,
        help=f"Maximum hits to return (default: {_SEARCH_DEFAULT_LIMIT}).",
    )
    search_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _add_inspect_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    inspect_p = sub.add_parser(
        "inspect",
        help="Show a session's header and message timeline.",
        epilog=(
            "session-id may be a full id or any unique prefix. "
            "Default: message content is truncated to 200 characters; pass "
            "--full to dump verbatim."
        ),
    )
    inspect_p.add_argument(
        "session_id",
        help="Session id or unique prefix to inspect.",
    )
    inspect_p.add_argument(
        "--full",
        action="store_true",
        help="Dump message content verbatim instead of truncating to 200 chars.",
    )
    inspect_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _add_snapshots_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    snapshots_p = sub.add_parser(
        "snapshots",
        help="List snapshot files in the snapshot directory (newest first).",
        epilog=(
            "The snapshot directory is $CONVO_BACKUP_DIR or "
            "<CONVO_DB>'s parent / convo-backups by default."
        ),
    )
    snapshots_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (RuntimeError, ValueError, FileExistsError, OSError, sqlite3.DatabaseError) as exc:
        print(f"convo: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args.db)

    handlers: dict[str, Callable[[argparse.Namespace, Path], int]] = {
        "backup": _backup_command,
        "restore": _restore_command,
        "index": _index_command,
        "info": _info_command,
        "search": _search_command,
        "inspect": _inspect_command,
        "snapshots": _snapshots_command,
    }
    handler = handlers.get(args.cmd)
    if handler is None:
        return 2
    rc: int = handler(args, db_path)
    return rc


def _backup_command(args: argparse.Namespace, db_path: Path) -> int:
    with Database(db_path) as db:
        if args.auto:
            written = db.backup_snapshot()
            print(f"snapshot written: {written}")
        else:
            db.backup(args.dest)
            print(f"backed up to {args.dest}")
    return 0


def _restore_command(args: argparse.Namespace, db_path: Path) -> int:
    src: Path = _resolve_restore_src(args, db_path)
    with Database(db_path) as db:
        db.restore_snapshot(src)
    print(f"restored from {src}")
    return 0


def _resolve_restore_src(args: argparse.Namespace, db_path: Path) -> Path:
    if args.latest:
        snapshot_dir = resolve_snapshot_dir(None, db_path)
        snapshots = list_snapshots(snapshot_dir)
        if not snapshots:
            msg = f"no snapshots in {snapshot_dir}"
            raise RuntimeError(msg)
        return snapshots[0].path
    # argparse's mutually-exclusive group with required=True guarantees
    # `args.src` is set when `--latest` is not.
    return cast("Path", args.src)


_SNAPSHOTS_ENVELOPE_VERSION: int = 1


def _snapshots_command(args: argparse.Namespace, db_path: Path) -> int:
    snapshot_dir = resolve_snapshot_dir(None, db_path)
    snapshots = list_snapshots(snapshot_dir)
    if args.as_json:
        print(json.dumps(_build_snapshots_envelope(snapshot_dir, snapshots)))
    else:
        _print_snapshots(snapshot_dir, snapshots)
    return 0


def _build_snapshots_envelope(
    snapshot_dir: Path,
    snapshots: list[SnapshotInfo],
) -> dict[str, object]:
    return {
        "schema_version": _SNAPSHOTS_ENVELOPE_VERSION,
        "snapshots": {
            "snapshot_dir": str(snapshot_dir),
            "snapshots": [_snapshot_to_dict(s) for s in snapshots],
        },
    }


def _snapshot_to_dict(s: SnapshotInfo) -> dict[str, object]:
    return {
        "name": s.path.name,
        "path": str(s.path),
        "timestamp_utc": s.timestamp_utc.isoformat(),
        "size_bytes": s.size_bytes,
        "age_human": s.age_human,
    }


def _print_snapshots(snapshot_dir: Path, snapshots: list[SnapshotInfo]) -> None:
    print(f"snapshot_dir  {snapshot_dir}")
    print()
    if not snapshots:
        print("(no snapshots)")
        return
    rows: list[tuple[str, str, str]] = [
        (s.path.name, _format_bytes(s.size_bytes), s.age_human) for s in snapshots
    ]
    name_w = max(len("name"), *(len(r[0]) for r in rows))
    size_w = max(len("size"), *(len(r[1]) for r in rows))
    age_w = max(len("age"), *(len(r[2]) for r in rows))
    print(f"{'name'.ljust(name_w)}  {'size'.rjust(size_w)}  {'age'.ljust(age_w)}")
    print(f"{'-' * name_w}  {'-' * size_w}  {'-' * age_w}")
    for name, size, age in rows:
        print(f"{name.ljust(name_w)}  {size.rjust(size_w)}  {age.ljust(age_w)}")


_REASON_TO_SUFFIX: dict[str, str] = {
    "empty": "empty",
    "unchanged": "skipped (unchanged)",
    "dry_run_unchanged": "would skip (unchanged)",
    "dry_run_new": "would index",
    "dry_run_force_reindex": "would re-index (--full)",
}


def _format_file_line(idx: int, total: int, projects_dir: Path, result: IndexResult) -> str:
    try:
        rel: Path = result.path.relative_to(projects_dir)
    except ValueError:
        rel = result.path
    prefix = f"{idx}/{total} {rel}"
    if result.error is not None:
        return f"{prefix}: FAILED: {result.error}"
    if result.skipped_reason is not None:
        suffix = _REASON_TO_SUFFIX.get(result.skipped_reason, result.skipped_reason)
        return f"{prefix}: {suffix}"
    msgs = result.inserted_rows.get("messages", 0)
    tcs = result.inserted_rows.get("tool_calls", 0)
    return f"{prefix}: indexed {msgs} messages / {tcs} tool_calls"


def _print_summary(report: IndexReport) -> None:
    secs = report.duration_ms / 1000.0
    print(
        f"Indexed {report.files_indexed} files "
        f"({report.files_skipped_unchanged} unchanged, "
        f"{report.files_skipped_empty} empty, "
        f"{report.files_failed} failed) "
        f"in {secs:.1f}s.",
    )
    rows = report.rows_inserted
    print(
        f"Inserted: {rows.get('messages', 0)} messages, "
        f"{rows.get('tool_calls', 0)} tool_calls, "
        f"{rows.get('tool_results', 0)} tool_results.",
    )
    if report.unknown_record_types:
        print(f"Unknown record types: {dict(report.unknown_record_types)}")


def _envelope_status(report: IndexReport) -> str:
    if report.files_failed == 0:
        return "success"
    if report.files_indexed == 0 and report.files_skipped_unchanged == 0:
        return "error"
    return "partial"


def _build_envelope(report: IndexReport) -> dict[str, object]:
    return {
        "status": _envelope_status(report),
        "files_seen": report.files_seen,
        "files_indexed": report.files_indexed,
        "files_skipped": report.files_skipped_unchanged + report.files_skipped_empty,
        "files_failed": report.files_failed,
        "rows_inserted": dict(report.rows_inserted),
        "unknown_record_types": dict(report.unknown_record_types),
        "errors": [
            {"path": str(path), "message": message, "line": line}
            for path, message, line in report.errors
        ],
        "duration_ms": report.duration_ms,
    }


def _index_command(args: argparse.Namespace, db_path: Path) -> int:
    projects_dir = _resolve_projects_dir(args.projects_dir)
    if not projects_dir.exists():
        msg = f"projects dir does not exist: {projects_dir}"
        raise RuntimeError(msg)
    if not projects_dir.is_dir():
        msg = f"projects dir is not a directory: {projects_dir}"
        raise RuntimeError(msg)

    from convo.intake.orchestrator import _discover_jsonl  # noqa: PLC0415

    total = len(_discover_jsonl(projects_dir))
    i = 0

    def progress(result: IndexResult) -> None:
        nonlocal i
        i += 1
        if not args.as_json:
            print(_format_file_line(i, total, projects_dir, result))

    with Database(db_path) as db:
        report = index_tree(
            db,
            projects_dir,
            full=args.full,
            dry_run=args.dry_run,
            on_file=progress,
        )

    if args.as_json:
        print(json.dumps(_build_envelope(report)))
    else:
        _print_summary(report)
    return 1 if _envelope_status(report) == "error" else 0


def _info_command(args: argparse.Namespace, db_path: Path) -> int:
    with Database(db_path) as db:
        report = gather_info(db)
    if args.as_json:
        print(json.dumps(_build_info_envelope(report)))
    else:
        _print_info(report)
    return 0


def _project_label(project: ProjectCount) -> str:
    if project.project_path is None or project.project_path == "":
        return _UNKNOWN_PROJECT_LABEL
    return project.project_path


def _build_info_envelope(report: InfoReport) -> dict[str, object]:
    return {
        "schema_version": _INFO_ENVELOPE_VERSION,
        "info": {
            "db_schema_version": report.schema_version,
            "row_counts": dict(report.row_counts),
            "last_indexed_at": report.last_indexed_at,
            "top_projects": [
                {"project_path": p.project_path, "session_count": p.session_count}
                for p in report.top_projects
            ],
            "db_size_bytes": report.db_size_bytes,
            "snapshot_dir_path": str(report.snapshot_dir_path),
            "snapshot_count": report.snapshot_count,
            "snapshot_total_bytes": report.snapshot_total_bytes,
        },
    }


def _format_bytes(n: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(n)
    for unit in units:
        if size < _BYTES_PER_KIB or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= _BYTES_PER_KIB
    # unreachable: the loop always returns because the last unit forces the branch
    raise AssertionError


def _print_info(report: InfoReport) -> None:
    rows: list[tuple[str, str]] = [
        ("schema_version", str(report.schema_version)),
        ("db_size", _format_bytes(report.db_size_bytes)),
        ("last_indexed_at", report.last_indexed_at or "(never)"),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"{label.ljust(width)}  {value}")

    print()
    print("row counts:")
    counts = report.row_counts
    count_width = max((len(table) for table in counts), default=0)
    for table, n in counts.items():
        print(f"  {table.ljust(count_width)}  {n}")

    print()
    print("top projects by sessions:")
    if not report.top_projects:
        print("  (no sessions)")
    else:
        for project in report.top_projects:
            label = _project_label(project)
            print(f"  {project.session_count:>6}  {label}")

    print()
    print("snapshots:")
    print(f"  dir          {report.snapshot_dir_path}")
    print(f"  count        {report.snapshot_count}")
    print(f"  total_bytes  {_format_bytes(report.snapshot_total_bytes)}")


def _search_command(args: argparse.Namespace, db_path: Path) -> int:
    with Database(db_path) as db:
        hits = list(
            search(
                db,
                args.query,
                since=args.since,
                project=args.project,
                tool=args.tool,
                limit=args.limit,
            ),
        )
    if args.as_json:
        print(json.dumps(_build_search_envelope(args, hits)))
    else:
        _print_search_hits(hits)
    return 0


def _build_search_envelope(args: argparse.Namespace, hits: list[SearchHit]) -> dict[str, object]:
    filters: dict[str, object] = {
        "since": _span_to_str(args.since),
        "project": args.project,
        "tool": args.tool,
        "limit": args.limit,
    }
    return {
        "schema_version": _SEARCH_ENVELOPE_VERSION,
        "search": {
            "query": args.query,
            "filters": filters,
            "hits": [_hit_to_dict(h) for h in hits],
        },
    }


def _hit_to_dict(hit: SearchHit) -> dict[str, object]:
    return {
        "kind": hit.kind,
        "id": hit.id,
        "session_id": hit.session_id,
        "timestamp": hit.timestamp,
        "excerpt": _strip_snippet_markers(hit.excerpt),
        "project": hit.project,
    }


def _span_to_str(span: timedelta | None) -> str | None:
    if span is None:
        return None
    total = int(span.total_seconds())
    return f"{total}s"


def _strip_snippet_markers(excerpt: str) -> str:
    return excerpt.replace(SNIPPET_PRE, "").replace(SNIPPET_POST, "")


def _render_excerpt_for_tty(excerpt: str) -> str:
    if sys.stdout.isatty():
        return excerpt.replace(SNIPPET_PRE, _ANSI_BOLD_ON).replace(SNIPPET_POST, _ANSI_BOLD_OFF)
    return _strip_snippet_markers(excerpt)


def _print_search_hits(hits: list[SearchHit]) -> None:
    if not hits:
        print("(no hits)")
        return
    for hit in hits:
        ts = hit.timestamp or "(no timestamp)"
        excerpt = _render_excerpt_for_tty(hit.excerpt)
        # Collapse newlines so each hit fits on one line.
        excerpt = excerpt.replace("\n", " ").replace("\r", " ")
        print(f"[{hit.kind}] {ts} | {excerpt} | {hit.session_id}")


def _inspect_command(args: argparse.Namespace, db_path: Path) -> int:
    with Database(db_path) as db:
        resolved = resolve_session_id(db, args.session_id)
        view = inspect_session(db, resolved)
    if args.as_json:
        print(json.dumps(_build_inspect_envelope(view, full=bool(args.full))))
    else:
        _print_inspect(view, full=bool(args.full))
    return 0


def _build_inspect_envelope(view: SessionView, *, full: bool) -> dict[str, object]:
    return {
        "schema_version": _INSPECT_ENVELOPE_VERSION,
        "inspect": {
            "session": {
                "id": view.id,
                "started_at": view.started_at,
                "ended_at": view.ended_at,
                "project_path": view.project_path,
                "model": view.model,
                "git_branch": view.git_branch,
            },
            "messages": [_message_to_dict(m, full=full) for m in view.messages],
        },
    }


def _message_to_dict(msg: MessageView, *, full: bool) -> dict[str, object]:
    content = msg.content if full else _truncate(msg.content, _INSPECT_PREVIEW_CHARS)
    return {
        "id": msg.id,
        "role": msg.role,
        "timestamp": msg.timestamp,
        "content": content,
        "truncated": (not full) and len(msg.content) > _INSPECT_PREVIEW_CHARS,
        "tool_calls": [_tool_call_to_dict(tc) for tc in msg.tool_calls],
    }


def _tool_call_to_dict(tc: ToolCallView) -> dict[str, object]:
    return {
        "id": tc.id,
        "name": tc.name,
        "input_json": tc.input_json,
        "started_at": tc.started_at,
    }


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "..."


_ROLE_ICONS: dict[str, str] = {
    "user": "U:",
    "assistant": "A:",
    "system": "S:",
}


def _role_icon(role: str) -> str:
    icon = _ROLE_ICONS.get(role)
    if icon is not None:
        return icon
    return f"{role}:"


def _print_inspect(view: SessionView, *, full: bool) -> None:
    print(f"session   {view.id}")
    print(f"started   {view.started_at or '(unknown)'}")
    print(f"ended     {view.ended_at or '(unknown)'}")
    print(f"project   {view.project_path or '(unknown)'}")
    print(f"model     {view.model or '(unknown)'}")
    print(f"branch    {view.git_branch or '(unknown)'}")
    print()
    if not view.messages:
        print("(no messages)")
        return
    print(f"messages  ({len(view.messages)} total)")
    for idx, msg in enumerate(view.messages, start=1):
        _print_message(idx, msg, full=full)


def _print_message(idx: int, msg: MessageView, *, full: bool) -> None:
    icon = _role_icon(msg.role)
    ts = msg.timestamp or "(no ts)"
    content = msg.content if full else _truncate(msg.content, _INSPECT_PREVIEW_CHARS)
    # Collapse multi-line content to one line in the default (non-full) view so the
    # numbered timeline stays scannable. With --full, preserve newlines verbatim.
    if not full:
        content = content.replace("\n", " ").replace("\r", " ")
    print(f"{idx}. {icon} {ts}  {content}")
    for tc in msg.tool_calls:
        preview = _truncate(tc.input_json.replace("\n", " "), _INSPECT_TOOL_INPUT_PREVIEW)
        print(f"  → {tc.name}: {preview}")
