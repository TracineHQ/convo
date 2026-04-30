"""Port a legacy convo DB (TracineHQ/claude-skills) to the new schema.

Boundary: this module does NOT honor the global `--db` flag. The
`migrate-legacy` subcommand owns its own `--src` / `--dest` paths so that
the same env precedence (`CONVO_DB`) can be reused for both, and the
default canonical case (`~/.claude/convo.db`) flows naturally through
`_resolve_paths`.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


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
