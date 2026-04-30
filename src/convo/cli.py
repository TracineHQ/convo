"""Minimal argparse CLI for convo: backup + restore."""

from __future__ import annotations

import argparse
from pathlib import Path

from convo.db import Database, resolve_db_path
from convo.legacy_migrate import run as _migrate_legacy_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="convo")
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
    backup.add_argument(
        "--prune",
        action="store_true",
        help="After writing, prune old snapshots (use with --auto).",
    )
    backup.add_argument(
        "--keep",
        type=int,
        default=7,
        help="Snapshots to retain when pruning (default: 7).",
    )

    restore = sub.add_parser(
        "restore",
        help="Restore the convo DB from a snapshot.",
    )
    restore.add_argument("src", type=Path)

    mig = sub.add_parser(
        "migrate-legacy",
        help="Port a legacy convo DB to the new schema.",
    )
    mig.add_argument(
        "--src",
        type=Path,
        default=None,
        help="Legacy DB path (default: $CONVO_DB or ~/.claude/convo.db).",
    )
    mig.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="New DB path (default: $CONVO_DB or ~/.claude/convo.db).",
    )
    mig.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate sources and report counts without writing the dest.",
    )
    mig.add_argument(
        "--no-keep-legacy",
        action="store_true",
        help="Disallow same-path migration (refuses to auto-rename).",
    )
    mig.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output instead of prose.",
    )
    mig.add_argument(
        "--seed",
        type=int,
        default=0xC0FFEE,
        help="Seed for stable validation sampling (default: 0xC0FFEE).",
    )
    mig.add_argument(
        "--resume-deferred",
        action="store_true",
        help="Re-run the deferred-table portion of a prior migration.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "migrate-legacy":
        return _migrate_legacy_run(args)

    db_path = resolve_db_path(args.db)

    if args.cmd == "backup":
        with Database(db_path) as db:
            if args.auto:
                written = db.backup_snapshot()
                print(f"snapshot written: {written}")
                if args.prune:
                    pruned = db.prune_snapshots(keep_n=args.keep)
                    print(f"pruned {len(pruned)} old snapshot(s)")
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
