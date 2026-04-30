"""Minimal argparse CLI for convo: backup + restore."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from convo.db import Database, resolve_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="convo",
        epilog=(
            "Environment variables: CONVO_DB (default DB path), "
            "CONVO_BACKUP_DIR (default snapshot directory, "
            "defaults to <CONVO_DB>'s parent / convo-backups)."
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

    return 2
