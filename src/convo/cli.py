"""Argparse CLI for convo: backup, restore, index, info, search, inspect, snapshots, stats."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from convo import __version__ as convo_version
from convo.analytics import (
    CommandsReport,
    Delta,
    DeltaReport,
    DiffReport,
    FilesReport,
    ModelReport,
    SessionsReport,
    SummaryReport,
    ToolsReport,
    WindowSnapshot,
    compute_diff,
    gather_summary,
    stats_commands,
    stats_files,
    stats_model,
    stats_sessions,
    stats_tools,
)
from convo.db import Database, resolve_db_path, resolve_snapshot_dir
from convo.intake.orchestrator import IndexReport, IndexResult, index_tree
from convo.read.filters import parse_span
from convo.read.info import InfoReport, ProjectCount, gather_info
from convo.read.inspect import (
    MessageView,
    SessionView,
    ToolCallView,
    inspect_session,
    resolve_latest_session,
    resolve_session_id,
)
from convo.read.search import SNIPPET_POST, SNIPPET_PRE, SearchHit, search
from convo.read.snapshots import SnapshotInfo, list_snapshots

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"

_INFO_ENVELOPE_VERSION: int = 1
_SEARCH_ENVELOPE_VERSION: int = 1
_INSPECT_ENVELOPE_VERSION: int = 1
_STATS_ENVELOPE_VERSION: int = 1
_INDEX_ENVELOPE_VERSION: int = 1
_BACKUP_ENVELOPE_VERSION: int = 1
_RESTORE_ENVELOPE_VERSION: int = 1
_ERROR_ENVELOPE_VERSION: int = 1
_STATS_FAMILIES: tuple[str, ...] = ("tools", "commands", "sessions", "files", "model")
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


def _positive_int(s: str) -> int:
    """Argparse type for `--limit`: rejects zero and negatives.

    Returns the parsed integer if it is >= 1; otherwise raises
    `argparse.ArgumentTypeError` so argparse exits 2 with a clean message
    instead of silently accepting nonsense values.
    """
    try:
        n = int(s)
    except ValueError as exc:
        msg = "--limit must be a positive integer"
        raise argparse.ArgumentTypeError(msg) from exc
    if n <= 0:
        msg = "--limit must be a positive integer"
        raise argparse.ArgumentTypeError(msg)
    return n


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
        "--version",
        action="version",
        version=f"convo {convo_version}",
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
    _add_stats_parser(sub)
    _add_summary_parser(sub)
    _add_diff_parser(sub)
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
    backup.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
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
    restore.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
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
            "use the shorthand <N><unit>: 7d, 24h, 90m, 30s, 2w, 1y."
        ),
    )
    search_p.add_argument("query", help="Search query string.")
    search_p.add_argument(
        "--since",
        type=parse_span,
        default=None,
        help="Only include hits newer than this span (e.g. 7d, 24h, 90m, 30s, 2w, 1y).",
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
        type=_positive_int,
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
    target = inspect_p.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "session_id",
        nargs="?",
        help="Session id or unique prefix to inspect.",
    )
    target.add_argument(
        "--latest",
        action="store_true",
        help="Inspect the most recently started session in the DB.",
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


def _add_stats_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    stats_p = sub.add_parser(
        "stats",
        help="Aggregate analytics over the indexed convo DB.",
        epilog=(
            "Family chooses which aggregation to run: "
            "tools (call frequency / median duration / error rate), "
            "commands (first-user-message histogram), "
            "sessions (count, median/p95 duration, hour-of-day), "
            "files (source_files counts and top-N by message_count), "
            "model (sessions per model)."
        ),
    )
    stats_p.add_argument(
        "family",
        choices=_STATS_FAMILIES,
        help="Which stats family to compute.",
    )
    stats_p.add_argument(
        "--since",
        type=parse_span,
        default=None,
        help="Only include rows newer than this span (e.g. 7d, 24h, 90m, 30s, 2w, 1y).",
    )
    stats_p.add_argument(
        "--project",
        default=None,
        help="Restrict to one project_path (exact match against sessions.project_path).",
    )
    stats_p.add_argument(
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
        if getattr(args, "as_json", False):
            envelope = {
                "schema_version": _ERROR_ENVELOPE_VERSION,
                "error": {"message": str(exc)},
            }
            print(json.dumps(envelope))
        else:
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
        "stats": _stats_command,
        "summary": _summary_command,
        "diff": _diff_command,
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
            snapshot_path = written
        else:
            db.backup(args.dest)
            snapshot_path = args.dest
    if args.as_json:
        print(json.dumps(_build_backup_envelope(snapshot_path)))
    elif args.auto:
        print(f"snapshot written: {snapshot_path}")
    else:
        print(f"backed up to {snapshot_path}")
    return 0


def _build_backup_envelope(snapshot_path: Path) -> dict[str, object]:
    size_bytes = snapshot_path.stat().st_size
    return {
        "schema_version": _BACKUP_ENVELOPE_VERSION,
        "backup": {
            "snapshot_path": str(snapshot_path),
            "size_bytes": size_bytes,
        },
    }


def _restore_command(args: argparse.Namespace, db_path: Path) -> int:
    src: Path = _resolve_restore_src(args, db_path)
    with Database(db_path) as db:
        db.restore_snapshot(src)
    if args.as_json:
        print(json.dumps(_build_restore_envelope(src)))
    else:
        print(f"restored from {src}")
    return 0


def _build_restore_envelope(src: Path) -> dict[str, object]:
    return {
        "schema_version": _RESTORE_ENVELOPE_VERSION,
        "restore": {"source": str(src)},
    }


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
            "entries": [_snapshot_to_dict(s) for s in snapshots],
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
        "schema_version": _INDEX_ENVELOPE_VERSION,
        "index": {
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
        },
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
        if args.latest:
            resolved = resolve_latest_session(db)
        else:
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


_STATS_FAMILY_FUNCS: dict[
    str,
    Callable[..., ToolsReport | CommandsReport | SessionsReport | FilesReport | ModelReport],
] = {
    "tools": stats_tools,
    "commands": stats_commands,
    "sessions": stats_sessions,
    "files": stats_files,
    "model": stats_model,
}


def _stats_command(args: argparse.Namespace, db_path: Path) -> int:
    family: str = args.family
    func = _STATS_FAMILY_FUNCS[family]
    with Database(db_path) as db:
        report = func(db, since=args.since, project=args.project)
    if args.as_json:
        print(json.dumps(_build_stats_envelope(family, report)))
    else:
        _print_stats(family, report)
    return 0


def _build_stats_envelope(
    family: str,
    report: ToolsReport | CommandsReport | SessionsReport | FilesReport | ModelReport,
) -> dict[str, object]:
    body: dict[str, object] = {"family": family, **dataclasses.asdict(report)}
    return {"schema_version": _STATS_ENVELOPE_VERSION, "stats": body}


def _print_stats(
    family: str,
    report: ToolsReport | CommandsReport | SessionsReport | FilesReport | ModelReport,
) -> None:
    if family == "tools":
        assert isinstance(report, ToolsReport)
        _print_stats_tools(report)
    elif family == "commands":
        assert isinstance(report, CommandsReport)
        _print_stats_commands(report)
    elif family == "sessions":
        assert isinstance(report, SessionsReport)
        _print_stats_sessions(report)
    elif family == "files":
        assert isinstance(report, FilesReport)
        _print_stats_files(report)
    elif family == "model":
        assert isinstance(report, ModelReport)
        _print_stats_model(report)


def _print_stats_tools(report: ToolsReport) -> None:
    print(f"total  {report.total}")
    if report.total == 0:
        print("(no data)")
        return
    print()
    print("top by frequency:")
    if not report.top_by_frequency:
        print("  (none)")
    else:
        name_w = max(len("tool"), *(len(f.name) for f in report.top_by_frequency))
        count_w = max(len("count"), *(len(str(f.count)) for f in report.top_by_frequency))
        print(f"  {'tool'.ljust(name_w)}  {'count'.rjust(count_w)}")
        print(f"  {'-' * name_w}  {'-' * count_w}")
        for f in report.top_by_frequency:
            print(f"  {f.name.ljust(name_w)}  {str(f.count).rjust(count_w)}")
    print()
    print("top by median duration:")
    if not report.top_by_median_duration:
        print("  (none)")
    else:
        rows = [
            (s.name, f"{s.median_ms:.1f}", str(s.sample_count))
            for s in report.top_by_median_duration
        ]
        name_w = max(len("tool"), *(len(r[0]) for r in rows))
        ms_w = max(len("median_ms"), *(len(r[1]) for r in rows))
        n_w = max(len("samples"), *(len(r[2]) for r in rows))
        print(f"  {'tool'.ljust(name_w)}  {'median_ms'.rjust(ms_w)}  {'samples'.rjust(n_w)}")
        print(f"  {'-' * name_w}  {'-' * ms_w}  {'-' * n_w}")
        for name, ms, n in rows:
            print(f"  {name.ljust(name_w)}  {ms.rjust(ms_w)}  {n.rjust(n_w)}")
    print()
    print("error rates:")
    if not report.error_rates:
        print("  (none)")
    else:
        rows2 = [
            (er.name, str(er.total), str(er.errors), f"{er.error_rate:.2%}")
            for er in report.error_rates
        ]
        name_w = max(len("tool"), *(len(r[0]) for r in rows2))
        tot_w = max(len("total"), *(len(r[1]) for r in rows2))
        err_w = max(len("errors"), *(len(r[2]) for r in rows2))
        rate_w = max(len("rate"), *(len(r[3]) for r in rows2))
        print(
            f"  {'tool'.ljust(name_w)}  "
            f"{'total'.rjust(tot_w)}  "
            f"{'errors'.rjust(err_w)}  "
            f"{'rate'.rjust(rate_w)}",
        )
        print(f"  {'-' * name_w}  {'-' * tot_w}  {'-' * err_w}  {'-' * rate_w}")
        for name, tot, err, rate in rows2:
            print(
                f"  {name.ljust(name_w)}  "
                f"{tot.rjust(tot_w)}  "
                f"{err.rjust(err_w)}  "
                f"{rate.rjust(rate_w)}",
            )


def _print_stats_commands(report: CommandsReport) -> None:
    print(f"total  {report.total}")
    if report.total == 0:
        print("(no data)")
        return
    print()
    print("top commands:")
    if not report.top_commands:
        print("  (none)")
        return
    count_w = max(len("count"), *(len(str(c.count)) for c in report.top_commands))
    print(f"  {'count'.rjust(count_w)}  command")
    print(f"  {'-' * count_w}  {'-' * 7}")
    for c in report.top_commands:
        print(f"  {str(c.count).rjust(count_w)}  {c.command}")


def _print_stats_sessions(report: SessionsReport) -> None:
    print(f"total                  {report.total}")
    print(f"sessions_with_duration {report.sessions_with_duration}")
    if report.total == 0:
        print("(no data)")
        return
    median = "n/a" if report.median_duration_s is None else f"{report.median_duration_s:.1f}s"
    p95 = "n/a" if report.p95_duration_s is None else f"{report.p95_duration_s:.1f}s"
    print(f"median_duration        {median}")
    print(f"p95_duration           {p95}")
    print()
    print("hour-of-day (UTC):")
    max_count = max(report.hour_of_day) if report.hour_of_day else 0
    width = max(len(str(max_count)), 1)
    for h, count in enumerate(report.hour_of_day):
        bar = "#" * count if count > 0 else ""
        print(f"  {h:02d}  {str(count).rjust(width)}  {bar}")


def _print_stats_files(report: FilesReport) -> None:
    print(f"total                {report.total}")
    print(f"total_size_bytes     {report.total_size_bytes}")
    print(f"total_message_count  {report.total_message_count}")
    if report.total == 0:
        print("(no data)")
        return
    print()
    print("top files by message_count:")
    if not report.top_files:
        print("  (none)")
        return
    rows = [(f.path, str(f.message_count), str(f.size_bytes)) for f in report.top_files]
    path_w = max(len("path"), *(len(r[0]) for r in rows))
    mc_w = max(len("messages"), *(len(r[1]) for r in rows))
    sz_w = max(len("size"), *(len(r[2]) for r in rows))
    print(f"  {'path'.ljust(path_w)}  {'messages'.rjust(mc_w)}  {'size'.rjust(sz_w)}")
    print(f"  {'-' * path_w}  {'-' * mc_w}  {'-' * sz_w}")
    for path, mc, sz in rows:
        print(f"  {path.ljust(path_w)}  {mc.rjust(mc_w)}  {sz.rjust(sz_w)}")


def _print_stats_model(report: ModelReport) -> None:
    print(f"total  {report.total}")
    print(f"null_count      {report.null_count}")
    if report.total == 0:
        print("(no data)")
        return
    print()
    print("by model:")
    if not report.by_model:
        print("  (none)")
        return
    name_w = max(len("model"), *(len(m.model) for m in report.by_model))
    count_w = max(len("sessions"), *(len(str(m.session_count)) for m in report.by_model))
    print(f"  {'model'.ljust(name_w)}  {'sessions'.rjust(count_w)}")
    print(f"  {'-' * name_w}  {'-' * count_w}")
    for m in report.by_model:
        print(f"  {m.model.ljust(name_w)}  {str(m.session_count).rjust(count_w)}")


_SUMMARY_ENVELOPE_VERSION: int = 1
_SUMMARY_TOP_LIMIT: int = 5


def _add_summary_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    summary_p = sub.add_parser(
        "summary",
        help="Compose all stats families into a single dashboard.",
        epilog=(
            "Runs each of the five stats families (tools, commands, sessions, "
            "files, model) over the same (since, project) window and prints a "
            "summary section per family."
        ),
    )
    summary_p.add_argument(
        "--since",
        type=parse_span,
        default=None,
        help="Only include rows newer than this span (e.g. 7d, 24h, 90m, 30s, 2w, 1y).",
    )
    summary_p.add_argument(
        "--project",
        default=None,
        help="Restrict to one project_path (exact match against sessions.project_path).",
    )
    summary_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _summary_command(args: argparse.Namespace, db_path: Path) -> int:
    with Database(db_path) as db:
        report = gather_summary(db, since=args.since, project=args.project)
    if args.as_json:
        print(json.dumps(_build_summary_envelope(report)))
    else:
        _print_summary_report(report)
    return 0


def _build_summary_envelope(report: SummaryReport) -> dict[str, object]:
    body: dict[str, object] = {
        "since": _span_to_str(report.since),
        "project": report.project,
        "tools": dataclasses.asdict(report.tools),
        "commands": dataclasses.asdict(report.commands),
        "sessions": dataclasses.asdict(report.sessions),
        "files": dataclasses.asdict(report.files),
        "model": dataclasses.asdict(report.model),
    }
    return {"schema_version": _SUMMARY_ENVELOPE_VERSION, "summary": body}


def _print_summary_report(report: SummaryReport) -> None:
    since_label = _span_to_str(report.since) or "all"
    project_label = report.project if report.project is not None else "all"
    print(f"convo summary (since={since_label}, project={project_label})")
    print()
    _print_summary_tools(report.tools)
    print()
    _print_summary_commands(report.commands)
    print()
    _print_summary_sessions(report.sessions)
    print()
    _print_summary_files(report.files)
    print()
    _print_summary_model(report.model)


def _print_summary_tools(report: ToolsReport) -> None:
    print(f"tools  ({report.total} calls)")
    if report.total == 0 or not report.top_by_frequency:
        print("  (no data)")
        return
    top = report.top_by_frequency[:_SUMMARY_TOP_LIMIT]
    name_w = max(len("tool"), *(len(f.name) for f in top))
    count_w = max(len("count"), *(len(str(f.count)) for f in top))
    print(f"  {'tool'.ljust(name_w)}  {'count'.rjust(count_w)}")
    print(f"  {'-' * name_w}  {'-' * count_w}")
    for f in top:
        print(f"  {f.name.ljust(name_w)}  {str(f.count).rjust(count_w)}")
    extra = len(report.top_by_frequency) - len(top)
    if extra > 0:
        print(f"  --- {extra} more")


def _print_summary_commands(report: CommandsReport) -> None:
    print(f"commands  ({report.total} sessions)")
    if report.total == 0 or not report.top_commands:
        print("  (no data)")
        return
    top = report.top_commands[:_SUMMARY_TOP_LIMIT]
    count_w = max(len("count"), *(len(str(c.count)) for c in top))
    print(f"  {'count'.rjust(count_w)}  command")
    print(f"  {'-' * count_w}  {'-' * 7}")
    for c in top:
        print(f"  {str(c.count).rjust(count_w)}  {c.command}")
    extra = len(report.top_commands) - len(top)
    if extra > 0:
        print(f"  --- {extra} more")


def _print_summary_sessions(report: SessionsReport) -> None:
    print(f"sessions  ({report.total} total)")
    if report.total == 0:
        print("  (no data)")
        return
    median = "n/a" if report.median_duration_s is None else f"{report.median_duration_s:.1f}s"
    p95 = "n/a" if report.p95_duration_s is None else f"{report.p95_duration_s:.1f}s"
    print(f"  median_duration  {median}")
    print(f"  p95_duration     {p95}")


def _print_summary_files(report: FilesReport) -> None:
    print(f"files  ({report.total} files, {report.total_message_count} messages)")
    if report.total == 0 or not report.top_files:
        print("  (no data)")
        return
    top = report.top_files[:_SUMMARY_TOP_LIMIT]
    rows = [(f.path, str(f.message_count)) for f in top]
    path_w = max(len("path"), *(len(r[0]) for r in rows))
    mc_w = max(len("messages"), *(len(r[1]) for r in rows))
    print(f"  {'path'.ljust(path_w)}  {'messages'.rjust(mc_w)}")
    print(f"  {'-' * path_w}  {'-' * mc_w}")
    for path, mc in rows:
        print(f"  {path.ljust(path_w)}  {mc.rjust(mc_w)}")
    extra = len(report.top_files) - len(top)
    if extra > 0:
        print(f"  --- {extra} more")


def _print_summary_model(report: ModelReport) -> None:
    print(f"model  ({report.total} sessions, {report.null_count} unknown)")
    if report.total == 0 or not report.by_model:
        print("  (no data)")
        return
    top = report.by_model[:_SUMMARY_TOP_LIMIT]
    name_w = max(len("model"), *(len(m.model) for m in top))
    count_w = max(len("sessions"), *(len(str(m.session_count)) for m in top))
    print(f"  {'model'.ljust(name_w)}  {'sessions'.rjust(count_w)}")
    print(f"  {'-' * name_w}  {'-' * count_w}")
    for m in top:
        print(f"  {m.model.ljust(name_w)}  {str(m.session_count).rjust(count_w)}")
    extra = len(report.by_model) - len(top)
    if extra > 0:
        print(f"  --- {extra} more")


_DIFF_ENVELOPE_VERSION: int = 1
_DIFF_DEFAULT_SPAN: timedelta = timedelta(days=7)
_DIFF_TOP_LIMIT: int = 10
_ANSI_GREEN: str = "\x1b[32m"
_ANSI_RED: str = "\x1b[31m"
_ANSI_RESET: str = "\x1b[0m"


def _add_diff_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    diff_p = sub.add_parser(
        "diff",
        help="Compare the current window against the previous window of the same length.",
        epilog=(
            "Default span is 7d. The current window is [now-span, now); the "
            "previous window is [now-2*span, now-span). Per-bucket deltas are "
            "shown alongside both windows. ANSI colour on TTY: green for "
            "increases, red for decreases."
        ),
    )
    diff_p.add_argument(
        "--since",
        type=parse_span,
        default=None,
        help="Window length (e.g. 7d, 24h, 90m, 30s, 2w, 1y). Default: 7d.",
    )
    diff_p.add_argument(
        "--project",
        default=None,
        help="Restrict to one project_path (exact match against sessions.project_path).",
    )
    diff_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _diff_command(args: argparse.Namespace, db_path: Path) -> int:
    span: timedelta = args.since if args.since is not None else _DIFF_DEFAULT_SPAN
    with Database(db_path) as db:
        report = compute_diff(db, span=span, project=args.project)
    if args.as_json:
        print(json.dumps(_build_diff_envelope(report)))
    else:
        _print_diff(report)
    return 0


def _build_diff_envelope(report: DiffReport) -> dict[str, object]:
    body: dict[str, object] = {
        "span_seconds": report.span_seconds,
        "project": report.project,
        "current": _window_to_dict(report.current),
        "previous": _window_to_dict(report.previous),
        "deltas": _deltas_to_dict(report.deltas),
    }
    return {"schema_version": _DIFF_ENVELOPE_VERSION, "diff": body}


def _window_to_dict(window: WindowSnapshot) -> dict[str, object]:
    return dataclasses.asdict(window)


def _delta_to_dict(delta: Delta) -> dict[str, object]:
    return {"absolute": delta.absolute, "pct": delta.pct}


def _deltas_to_dict(deltas: DeltaReport) -> dict[str, object]:
    return {
        "tool_calls_total": _delta_to_dict(deltas.tool_calls_total),
        "tool_calls_by_name": {k: _delta_to_dict(v) for k, v in deltas.tool_calls_by_name.items()},
        "commands_total": _delta_to_dict(deltas.commands_total),
        "commands_top": {k: _delta_to_dict(v) for k, v in deltas.commands_top.items()},
        "sessions_count": _delta_to_dict(deltas.sessions_count),
        "sessions_median_seconds": _delta_to_dict(deltas.sessions_median_seconds),
        "sessions_p95_seconds": _delta_to_dict(deltas.sessions_p95_seconds),
        "files_count": _delta_to_dict(deltas.files_count),
        "model_histogram": {k: _delta_to_dict(v) for k, v in deltas.model_histogram.items()},
    }


def _diff_use_color() -> bool:
    return sys.stdout.isatty()


def _format_delta_int(delta: Delta, *, color: bool) -> str:
    abs_val = int(delta.absolute) if float(delta.absolute).is_integer() else delta.absolute
    pct_str = _format_pct(delta.pct)
    sign = "+" if delta.absolute > 0 else ""
    raw = f"{sign}{abs_val} ({pct_str})"
    return _wrap_color(raw, delta.absolute, color=color)


def _format_delta_float(delta: Delta, *, color: bool, suffix: str = "") -> str:
    pct_str = _format_pct(delta.pct)
    sign = "+" if delta.absolute > 0 else ""
    raw = f"{sign}{delta.absolute:.1f}{suffix} ({pct_str})"
    return _wrap_color(raw, delta.absolute, color=color)


def _wrap_color(text: str, value: float, *, color: bool) -> str:
    if not color or value == 0:
        return text
    code = _ANSI_GREEN if value > 0 else _ANSI_RED
    return f"{code}{text}{_ANSI_RESET}"


def _format_pct(pct: float | None) -> str:
    if pct is None:
        return "new"
    return f"{pct * 100:+.1f}%"


def _print_diff(report: DiffReport) -> None:
    color = _diff_use_color()
    span_label = _span_seconds_label(report.span_seconds)
    project_label = report.project if report.project is not None else "all"
    print(f"convo diff (span={span_label}, project={project_label})")
    print(f"  current   [{report.current.lower}, {report.current.upper})")
    print(f"  previous  [{report.previous.lower}, {report.previous.upper})")
    print()
    _print_diff_scalars(report, color=color)
    print()
    _print_diff_mapping(
        "top tools by frequency",
        current=report.current.tool_calls_by_name,
        previous=report.previous.tool_calls_by_name,
        deltas=report.deltas.tool_calls_by_name,
        color=color,
    )
    print()
    _print_diff_mapping(
        "top commands",
        current=report.current.commands_top,
        previous=report.previous.commands_top,
        deltas=report.deltas.commands_top,
        color=color,
    )
    print()
    _print_diff_mapping(
        "model histogram",
        current=report.current.model_histogram,
        previous=report.previous.model_histogram,
        deltas=report.deltas.model_histogram,
        color=color,
    )


def _print_diff_scalars(report: DiffReport, *, color: bool) -> None:
    rows: list[tuple[str, str, str, str]] = [
        (
            "tool_calls_total",
            str(report.current.tool_calls_total),
            str(report.previous.tool_calls_total),
            _format_delta_int(report.deltas.tool_calls_total, color=color),
        ),
        (
            "commands_total",
            str(report.current.commands_total),
            str(report.previous.commands_total),
            _format_delta_int(report.deltas.commands_total, color=color),
        ),
        (
            "sessions_count",
            str(report.current.sessions_count),
            str(report.previous.sessions_count),
            _format_delta_int(report.deltas.sessions_count, color=color),
        ),
        (
            "sessions_median_s",
            _opt_seconds(report.current.sessions_median_seconds),
            _opt_seconds(report.previous.sessions_median_seconds),
            _format_delta_float(report.deltas.sessions_median_seconds, color=color, suffix="s"),
        ),
        (
            "sessions_p95_s",
            _opt_seconds(report.current.sessions_p95_seconds),
            _opt_seconds(report.previous.sessions_p95_seconds),
            _format_delta_float(report.deltas.sessions_p95_seconds, color=color, suffix="s"),
        ),
        (
            "files_count",
            str(report.current.files_count),
            str(report.previous.files_count),
            _format_delta_int(report.deltas.files_count, color=color),
        ),
    ]
    metric_w = max(len("metric"), *(len(r[0]) for r in rows))
    cur_w = max(len("current"), *(len(r[1]) for r in rows))
    prev_w = max(len("previous"), *(len(r[2]) for r in rows))
    delta_label = "Δ"
    print(
        f"{'metric'.ljust(metric_w)}  "
        f"{'current'.rjust(cur_w)}  "
        f"{'previous'.rjust(prev_w)}  "
        f"{delta_label}",
    )
    print(f"{'-' * metric_w}  {'-' * cur_w}  {'-' * prev_w}  {'-' * 7}")
    for metric, cur, prev, delta in rows:
        print(f"{metric.ljust(metric_w)}  {cur.rjust(cur_w)}  {prev.rjust(prev_w)}  {delta}")


def _print_diff_mapping(
    title: str,
    *,
    current: object,
    previous: object,
    deltas: object,
    color: bool,
) -> None:
    cur_map = cast("dict[str, int]", current)
    prev_map = cast("dict[str, int]", previous)
    delta_map = cast("dict[str, Delta]", deltas)
    print(f"{title}:")
    if not delta_map:
        print("  (no data)")
        return
    items = list(delta_map.items())[:_DIFF_TOP_LIMIT]
    rows = [
        (k, str(cur_map.get(k, 0)), str(prev_map.get(k, 0)), _format_delta_int(d, color=color))
        for k, d in items
    ]
    name_w = max(len("name"), *(len(r[0]) for r in rows))
    cur_w = max(len("current"), *(len(r[1]) for r in rows))
    prev_w = max(len("previous"), *(len(r[2]) for r in rows))
    delta_label = "Δ"
    print(
        f"  {'name'.ljust(name_w)}  "
        f"{'current'.rjust(cur_w)}  "
        f"{'previous'.rjust(prev_w)}  "
        f"{delta_label}",
    )
    print(f"  {'-' * name_w}  {'-' * cur_w}  {'-' * prev_w}  {'-' * 7}")
    for name, cur, prev, delta in rows:
        print(f"  {name.ljust(name_w)}  {cur.rjust(cur_w)}  {prev.rjust(prev_w)}  {delta}")
    extra = len(delta_map) - len(rows)
    if extra > 0:
        print(f"  --- {extra} more")


def _opt_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}s"


def _span_seconds_label(span_seconds: float) -> str:
    total = int(span_seconds)
    return f"{total}s"
