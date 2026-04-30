"""Argparse CLI for convo: backup, restore, index."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from convo.db import Database, resolve_db_path
from convo.intake.orchestrator import IndexReport, IndexResult, index_tree

DEFAULT_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"


def _resolve_projects_dir(explicit: Path | str | None = None) -> Path:
    """Resolve projects dir with precedence: explicit > $CLAUDE_PROJECTS_DIR > default."""
    if explicit is not None:
        return Path(explicit).expanduser()
    env = os.environ.get("CLAUDE_PROJECTS_DIR")
    if env:
        return Path(env).expanduser()
    return DEFAULT_PROJECTS_DIR


def main(argv: list[str] | None = None) -> int:
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

    restore = sub.add_parser(
        "restore",
        help="Restore the convo DB from a snapshot. The snapshot file is preserved.",
    )
    restore.add_argument("src", type=Path)

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

    args = parser.parse_args(argv)

    try:
        return _dispatch(args)
    except (RuntimeError, ValueError, FileExistsError, OSError, sqlite3.DatabaseError) as exc:
        print(f"convo: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args.db)

    if args.cmd == "backup":
        with Database(db_path) as db:
            if args.auto:
                written = db.backup_snapshot()
                print(f"snapshot written: {written}")
            else:
                db.backup(args.dest)
                print(f"backed up to {args.dest}")
        return 0

    if args.cmd == "restore":
        with Database(db_path) as db:
            db.restore_snapshot(args.src)
        print(f"restored from {args.src}")
        return 0

    if args.cmd == "index":
        return _index_command(args, db_path)

    return 2


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
    counter = {"i": 0}

    def progress(result: IndexResult) -> None:
        counter["i"] += 1
        if not args.as_json:
            print(_format_file_line(counter["i"], total, projects_dir, result))

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
