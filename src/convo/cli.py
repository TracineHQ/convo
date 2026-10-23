"""Argparse CLI for convo.

Subcommands: backup, restore, index, index-guard, migrate, hook, info, search,
inspect, snapshots, stats, summary, diff, projects, tools, sessions.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, override

from convo import __version__ as convo_version
from convo.analytics import (
    CommandsReport,
    Delta,
    DeltaReport,
    DiffReport,
    FilesReport,
    HooksReport,
    ModelReport,
    SessionsReport,
    SummaryReport,
    ToolErrorRate,
    ToolFreq,
    ToolsReport,
    WindowSnapshot,
    compute_diff,
    gather_summary,
    stats_commands,
    stats_files,
    stats_hooks,
    stats_model,
    stats_sessions,
    stats_tools,
)
from convo.cli_errors import precheck_argv
from convo.db import (
    READ_MIN_SCHEMA_VERSION,
    SCHEMA_VERSION,
    AppliedMigration,
    Database,
    resolve_db_path,
    resolve_snapshot_dir,
)
from convo.hook import HookOutcome, run_pre_compact, run_session_end
from convo.intake.guard import index_guard_file, resolve_guard_log_path
from convo.intake.orchestrator import (
    IndexReport,
    IndexResult,
    discover_transcripts,
    index_tree,
    is_valid_session_id,
)
from convo.lock import WaiterLock, WriterLock
from convo.read._db_access import open_ro as _open_ro
from convo.read.filters import ProjectResolveError, parse_span, resolve_project_path, since_iso
from convo.read.info import InfoReport, ProjectCount, gather_info
from convo.read.inspect import (
    MessageView,
    SessionView,
    ToolCallView,
    build_timeline,
    inspect_session,
    resolve_latest_session,
    resolve_session_id,
)
from convo.read.projects import ProjectRow, list_projects
from convo.read.prose import (
    PROJECTION_FIELDS,
    SearchRenderConfig,
    TimelineEvent,
    render_search_hits,
    render_timeline,
)
from convo.read.search import (
    SNIPPET_MAX_TOKENS,
    SNIPPET_POST,
    SNIPPET_PRE,
    SearchHit,
    count_matches,
    extract_indices_and_clean,
    search,
)
from convo.read.sessions import SessionRow, list_sessions
from convo.read.snapshots import SnapshotInfo, list_snapshots
from convo.read.suggestions import Suggestion, generate_suggestions
from convo.read.tools import ToolRow, list_tools

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# referenced below in _projects_command, _tools_command, _sessions_command
_DISCOVERY_REFS = (ProjectRow, list_projects, SessionRow, list_sessions, ToolRow, list_tools)

DEFAULT_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"

INFO_ENVELOPE_VERSION: int = 2
SEARCH_ENVELOPE_VERSION: int = 2
INSPECT_ENVELOPE_VERSION: int = 2
STATS_ENVELOPE_VERSION: int = 2
INDEX_ENVELOPE_VERSION: int = 2
BACKUP_ENVELOPE_VERSION: int = 2
RESTORE_ENVELOPE_VERSION: int = 2
ERROR_ENVELOPE_VERSION: int = 2
PROJECTS_ENVELOPE_VERSION: int = 2
TOOLS_ENVELOPE_VERSION: int = 2
SESSIONS_ENVELOPE_VERSION: int = 2
MIGRATE_ENVELOPE_VERSION: int = 2
HOOK_ENVELOPE_VERSION: int = 2
_STATS_FAMILIES: tuple[str, ...] = ("tools", "commands", "sessions", "files", "model", "hooks")
_INSPECT_PREVIEW_CHARS: int = 200
_TIMELINE_PREVIEW_CHARS: int = 80
_INSPECT_TOOL_INPUT_PREVIEW: int = 80
_UNKNOWN_PROJECT_LABEL: str = "(unknown)"
_BYTES_PER_KIB: int = 1024
_SEARCH_DEFAULT_LIMIT: int = 10
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


@contextmanager
def _read_db(db_path: Path) -> Iterator[Database]:
    """Open the DB for a read command: never migrate, refuse a too-old schema.

    Pending migrations the read path does not need are left for `convo
    migrate` (or the next `convo index`), so a read never waits on a slow
    one. A DB older than `READ_MIN_SCHEMA_VERSION` raises
    `MigrationRequiredError`, which `main` reports as the error envelope.
    """
    with Database(db_path, migrate=False) as db:
        db.require_schema(READ_MIN_SCHEMA_VERSION)
        yield db


@contextmanager
def _writer_lock(db_path: Path, *, wait: bool = True) -> Iterator[bool]:
    """Hold the DB's writer lock for the block; yield whether it was taken.

    With `wait`, block until another writer (often the SessionEnd hook's
    background run) finishes; otherwise yield False at once if it is held.
    """
    lock = WriterLock(db_path)
    acquired = lock.acquire(blocking=False)
    if not acquired and wait:
        print("convo: waiting for another convo writer to finish ...", file=sys.stderr, flush=True)
        acquired = lock.acquire(blocking=True)
    try:
        yield acquired
    finally:
        lock.release()


# How long a hook run that is next in line waits for the writer lock before
# giving up. Generous: a first full index of a large history can take a while.
_HOOK_RUN_MAX_WAIT_SECONDS: float = 3600.0


@contextmanager
def _index_lock(args: argparse.Namespace, db_path: Path) -> Iterator[str | None]:
    """Take the lock `convo index` needs; yield how it got it.

    Yields "now" (go ahead with the requested scope), "after_wait" (a hook run
    that waited: index every transcript) or None (not taken: do nothing).
    """
    if args.dry_run:
        # A dry run only reads, so it neither waits for the writer lock nor migrates.
        yield "now"
    elif args.hook_run:
        with _hook_run_lock(db_path) as grant:
            yield grant
    else:
        with _writer_lock(db_path, wait=not args.no_wait) as acquired:
            yield "now" if acquired else None


@contextmanager
def _hook_run_lock(db_path: Path) -> Iterator[str | None]:
    """The writer lock for the SessionEnd hook's background run, one waiter at most.

    A free lock is taken at once ("now": index just this session). A held one
    means waiting, but only one run waits: it takes the `WaiterLock` slot, and
    a run that finds the slot taken exits (None), because the waiter indexes
    every transcript once it gets the lock ("after_wait"), covering each
    session that ended meanwhile. The waiter frees the slot the moment it
    stops waiting, since its discovery comes after that and a session ending
    during its run needs the next waiter. Waiting is capped (None).
    """
    writer = WriterLock(db_path)
    if writer.acquire(blocking=False):
        grant = "now"
    else:
        waiter = WaiterLock(db_path)
        if not waiter.acquire(blocking=False):
            print(
                "convo: another convo writer is running; queued behind an existing waiter, "
                "which will index this session",
                file=sys.stderr,
            )
            yield None
            return
        try:
            print(
                "convo: waiting for another convo writer to finish ...",
                file=sys.stderr,
                flush=True,
            )
            acquired = writer.acquire(blocking=True, timeout=_HOOK_RUN_MAX_WAIT_SECONDS)
        finally:
            waiter.release()
        if not acquired:
            print(
                f"convo: gave up after waiting {_HOOK_RUN_MAX_WAIT_SECONDS:.0f}s for another "
                "convo writer; run `convo index` to catch up",
                file=sys.stderr,
            )
            yield None
            return
        grant = "after_wait"
    try:
        yield grant
    finally:
        writer.release()


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


def _session_id_arg(s: str) -> str:
    """Argparse type for `index --session`: a bare session id, never a path or glob."""
    if not is_valid_session_id(s):
        msg = f"invalid session id {s!r}: expected letters, digits, '.', '_', '-'"
        raise argparse.ArgumentTypeError(msg)
    return s


def _message_number(s: str) -> int:
    """Argparse type for `--from-message` / `--to-message`: 1-indexed, >= 1."""
    try:
        n = int(s)
    except ValueError as exc:
        msg = "message numbers are 1-indexed integers (>= 1)"
        raise argparse.ArgumentTypeError(msg) from exc
    if n < 1:
        msg = "message numbers are 1-indexed integers (>= 1)"
        raise argparse.ArgumentTypeError(msg)
    return n


def _fields_list(s: str) -> list[str]:
    """Argparse type for `search --fields`: comma-separated known field names."""
    names = [name.strip() for name in s.split(",") if name.strip()]
    unknown = [name for name in names if name not in PROJECTION_FIELDS]
    if not names or unknown:
        shown = ", ".join(repr(name) for name in unknown) or repr(s)
        msg = f"unknown --fields name {shown}; valid: {','.join(PROJECTION_FIELDS)}"
        raise argparse.ArgumentTypeError(msg)
    return names


def _non_negative_int(msg: str) -> Callable[[str], int]:
    """Build an argparse type that accepts 0 or a positive integer.

    Rejections raise `argparse.ArgumentTypeError(msg)` so argparse exits 2.
    """

    def parse(s: str) -> int:
        try:
            n = int(s)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(msg) from exc
        if n < 0:
            raise argparse.ArgumentTypeError(msg)
        return n

    return parse


def _version_string() -> str:
    """Multi-line ``--version`` payload — name, version, install path, repo URL.

    Mirrors `gh --version`. Concrete install path makes bug reports
    self-identifying (which wheel? editable? site-packages?).
    """
    install_dir = str(Path(__file__).resolve().parent)
    return (
        f"convo {convo_version}\n"
        f"tracine-convo from {install_dir}\n"
        "https://github.com/TracineHQ/convo"
    )


class _RawVersionAction(argparse.Action):
    """Print ``--version`` text verbatim (preserves newlines).

    argparse's built-in ``version`` action runs the string through
    ``HelpFormatter._fill_text`` which collapses newlines to spaces.
    """

    def __init__(
        self,
        option_strings: list[str],
        dest: str = argparse.SUPPRESS,
        default: str = argparse.SUPPRESS,
        version: str = "",
        help: str = "show program's version number and exit",  # noqa: A002
    ) -> None:
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            default=default,
            nargs=0,
            help=help,
        )
        self._version = version

    @override
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del namespace, values, option_string
        sys.stdout.write(self._version + "\n")
        parser.exit(0)


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
        action=_RawVersionAction,
        version=_version_string(),
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
    _add_index_guard_parser(sub)
    _add_migrate_parser(sub)
    _add_hook_parser(sub)
    _add_info_parser(sub)
    _add_search_parser(sub)
    _add_inspect_parser(sub)
    _add_snapshots_parser(sub)
    _add_stats_parser(sub)
    _add_summary_parser(sub)
    _add_diff_parser(sub)
    _add_projects_parser(sub)
    _add_tools_parser(sub)
    _add_sessions_parser(sub)
    return parser


def _add_backup_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    backup = sub.add_parser(
        "backup",
        help="Snapshot the convo DB.",
        epilog=(
            "--auto writes to $CONVO_BACKUP_DIR (default: <CONVO_DB>'s parent / convo-backups). "
            "Snapshot files are 0600. CONVO_DB selects the source DB."
        ),
    )
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
        epilog=(
            "--latest picks the newest file in $CONVO_BACKUP_DIR (default: "
            "<CONVO_DB>'s parent / convo-backups). CONVO_DB selects the destination DB."
        ),
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
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Walks <projects-dir>/<slug>/*.jsonl, most recently modified first, plus\n"
            "each session's subagent transcripts under <slug>/<session>/subagents/.\n"
            "Each subagent is a session of its own, with the id\n"
            "<agent-id>.<parent-session-id>.\n"
            "Skips files whose sha256 is already recorded in source_files. With\n"
            "--full, every file is re-indexed. CLAUDE_PROJECTS_DIR overrides the\n"
            "default projects dir.\n"
            "\n"
            "Examples:\n"
            "  convo index\n"
            "  convo index --full --json\n"
            "  convo index --projects-dir /path/to/projects --dry-run\n"
            "  convo index --session <session-id>\n"
        ),
    )
    index_p.add_argument(
        "--projects-dir",
        type=Path,
        default=None,
        help="Projects dir (default: $CLAUDE_PROJECTS_DIR or ~/.claude/projects/).",
    )
    index_p.add_argument(
        "--session",
        type=_session_id_arg,
        default=None,
        metavar="ID",
        help=(
            "Index only <projects-dir>/<slug>/<ID>.jsonl and that session's subagent "
            "transcripts (a full session id, not a prefix)."
        ),
    )
    index_p.add_argument(
        "--no-wait",
        action="store_true",
        help=(
            "If another convo writer is running, exit 0 without indexing "
            "(default: wait for it to finish)."
        ),
    )
    index_p.add_argument(
        "--no-migrate",
        action="store_true",
        help="Refuse to run if the DB has pending migrations instead of applying them.",
    )
    # Internal: the SessionEnd hook's background run (see `_hook_run_lock`).
    index_p.add_argument("--hook-run", action="store_true", help=argparse.SUPPRESS)
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


def _add_index_guard_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    guard_p = sub.add_parser(
        "index-guard",
        help="Index guard's JSONL decision log into the convo DB.",
        epilog=(
            "Path resolution: --path > $GUARD_DECISIONS_PATH > "
            "~/.claude/guard-decisions.jsonl. Idempotent on unchanged files."
        ),
    )
    guard_p.add_argument(
        "--path",
        type=Path,
        default=None,
        help=(
            "Explicit guard JSONL path "
            "(default: $GUARD_DECISIONS_PATH or ~/.claude/guard-decisions.jsonl)."
        ),
    )
    guard_p.add_argument(
        "--full",
        action="store_true",
        help="Re-index regardless of recorded sha256.",
    )
    guard_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _add_migrate_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    migrate_p = sub.add_parser(
        "migrate",
        help="Apply pending schema migrations to the convo DB.",
        epilog=(
            "Read commands never migrate; `convo index` (including the SessionEnd hook's "
            "background run) and this command do. On a large DB a migration can take "
            "minutes; it holds the write lock, and reads keep working. Afterwards, "
            "`convo index` catches up. --backup first writes a snapshot to the "
            "snapshot directory ($CONVO_BACKUP_DIR, default <CONVO_DB>'s parent / "
            "convo-backups), which `convo restore --latest` restores. It only does so "
            "while a migration is pending, and the hook's background run usually "
            "migrates first: to keep a way back, run `convo backup --auto` with your "
            "current CLI before upgrading."
        ),
    )
    migrate_p.add_argument(
        "--backup",
        action="store_true",
        help="Snapshot the DB before applying migrations (skipped when none are pending).",
    )
    migrate_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _add_hook_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    hook_p = sub.add_parser(
        "hook",
        help="Entry point for Claude Code hooks; reads the hook's JSON on stdin.",
        epilog=(
            "session-end starts `convo index --session <id>` in the background for the "
            "session that just ended and returns at once, inside Claude Code's 1.5 s "
            "SessionEnd budget. That run's output is appended to <CONVO_DB>.hook.log. If "
            "another convo writer is running, one background run waits for it (up to an "
            "hour) and then indexes every transcript; later ones leave their sessions to "
            "it. Like any `convo index`, it applies pending migrations first. "
            "pre-compact does the same for a session that is still running, just before "
            "Claude Code compacts it, so a session that lasts days stays searchable. Its "
            "background run uses `convo index --session <id> --no-wait`: if another convo "
            "writer is running it logs that and exits."
        ),
    )
    hook_p.add_argument(
        "event",
        choices=["session-end", "pre-compact"],
        help="The hook event to handle.",
    )
    hook_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _add_info_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    info_p = sub.add_parser(
        "info",
        help="Print an overview of the convo DB (row counts, last index time, snapshots).",
        epilog=(
            "Reports row counts per table, the most recent index timestamp, "
            "and the snapshot directory contents. Read-only; no DB writes."
        ),
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
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Tokens are ANDed by default across whitespace-separated terms;\n"
            'quote literals as "phrase" for phrase search; OR keyword between\n'
            "terms; -token excludes. --since takes <N><unit> (7d, 24h, 90m,\n"
            "30s, 2w, 1y) or an ISO date (2026-04-01).\n"
            "\n"
            "Examples:\n"
            "  convo search 'TypeError'\n"
            "  convo search 'flaky test' --since 7d\n"
            "  convo search 'migration -rollback' --tool Bash\n"
            "  convo search 'auth' --project /Users/dev/myapp --format json\n"
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
        help="Fuzzy match: exact path, basename, or substring (see `convo projects`).",
    )
    search_p.add_argument(
        "--tool",
        default=None,
        help="Prefix match by default; use --tool-exact for strict equality.",
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
        help="Alias for --format=json (deprecated, use --format=json).",
    )
    search_p.add_argument(
        "--format",
        choices=["prose", "json"],
        default="prose",
        help="output format (default: prose)",
    )
    search_p.add_argument(
        "--excerpt-chars",
        type=_non_negative_int("--excerpt-chars must be 0 or a positive integer"),
        default=SNIPPET_MAX_TOKENS,
        help=(
            f"excerpt width in characters around the match (default and maximum: "
            f"{SNIPPET_MAX_TOKENS}; FTS5 caps snippets at about {SNIPPET_MAX_TOKENS} "
            "characters). For full text pass the hit's position to "
            "`convo inspect <session> --from-message P --to-message P --max-chars 0`"
        ),
    )
    search_p.add_argument(
        "--session",
        type=str,
        default=None,
        help="restrict to a session id (prefix accepted)",
    )
    search_p.add_argument(
        "--tool-exact",
        action="store_true",
        help="match --tool exactly instead of prefix",
    )
    search_p.add_argument(
        "--fields",
        type=_fields_list,
        default=None,
        help=(
            "print one tab-separated line per hit with these comma-separated fields "
            f"(prose mode only): {','.join(PROJECTION_FIELDS)}"
        ),
    )


def _add_inspect_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    inspect_p = sub.add_parser(
        "inspect",
        help="Show a session's header and message timeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "session-id may be a full id or any unique prefix.\n"
            "Default: output is capped at 50 messages; pass --full to disable\n"
            "the cap and return all messages in the session.\n"
            f"Message content is clipped to {_INSPECT_PREVIEW_CHARS} code points\n"
            f"({_TIMELINE_PREVIEW_CHARS} with --timeline); --max-chars 0 shows it in full.\n"
            "\n"
            "Examples:\n"
            "  convo inspect abc123\n"
            "  convo inspect --latest\n"
            "  convo inspect abc123 --full --json\n"
            "  convo inspect abc123 --from-message 12 --to-message 14 --max-chars 0\n"
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
        help="Disable the 50-message cap; return all messages in the session.",
    )
    inspect_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )
    inspect_p.add_argument(
        "--timeline",
        action="store_true",
        help="Render the session as a chronological timeline.",
    )
    inspect_p.add_argument(
        "--from-message",
        type=_message_number,
        default=None,
        dest="from_message",
        help="Start at the Nth message (1-indexed).",
    )
    inspect_p.add_argument(
        "--to-message",
        type=_message_number,
        default=None,
        dest="to_message",
        help="End at the Nth message (1-indexed, inclusive; clamped to the last message).",
    )
    inspect_p.add_argument(
        "--max-chars",
        type=_non_negative_int("--max-chars must be 0 (no limit) or a positive integer"),
        default=None,
        dest="max_chars",
        help=(
            "Clip each message's content and tool-call input to N code points "
            f"(default: {_INSPECT_PREVIEW_CHARS} for content, {_INSPECT_TOOL_INPUT_PREVIEW} "
            "for tool input, which is unclipped in --json; "
            f"{_TIMELINE_PREVIEW_CHARS} for both with --timeline). 0 means no limit."
        ),
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
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Family chooses which aggregation to run:\n"
            "  tools     call frequency / error rate\n"
            "  commands  first-user-message histogram\n"
            "  sessions  count, median/p95 duration, hour-of-day\n"
            "  files     source_files counts and top-N by message_count\n"
            "  model     sessions per model\n"
            "  hooks     guard decision counts by hook_id and decision\n"
            "\n"
            "commands, sessions and model count top-level sessions only; add\n"
            "--include-subagents to count subagent sessions too. tools and files\n"
            "always include subagent work, and hooks (the guard log) has no\n"
            "sessions to include.\n"
            "\n"
            "Examples:\n"
            "  convo stats tools\n"
            "  convo stats sessions --since 7d\n"
            "  convo stats hooks --project /Users/dev/myapp --json\n"
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
        help="Fuzzy match: exact path, basename, or substring (see `convo projects`).",
    )
    _add_include_subagents_arg(
        stats_p,
        "Count subagent sessions in the commands, sessions and model families "
        "(tools and files always include subagent work; hooks is unaffected).",
    )
    stats_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _add_include_subagents_arg(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument(
        "--include-subagents",
        dest="include_subagents",
        action="store_true",
        help=help_text,
    )


def main(argv: list[str] | None = None) -> int:

    precheck_argv(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (RuntimeError, ValueError, FileExistsError, OSError, sqlite3.DatabaseError) as exc:
        if getattr(args, "as_json", False) or getattr(args, "format", None) == "json":
            envelope = {
                "schema_version": ERROR_ENVELOPE_VERSION,
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
        "index-guard": _index_guard_command,
        "migrate": _migrate_command,
        "hook": _hook_command,
        "info": _info_command,
        "search": _search_command,
        "inspect": _inspect_command,
        "snapshots": _snapshots_command,
        "stats": _stats_command,
        "summary": _summary_command,
        "diff": _diff_command,
        "projects": _projects_command,
        "tools": _tools_command,
        "sessions": _sessions_command,
    }
    handler = handlers.get(args.cmd)
    if handler is None:
        return 2
    rc: int = handler(args, db_path)
    return rc


def _announce_snapshot() -> None:
    # stderr: stdout stays the single result line or JSON envelope.
    print(
        "convo: writing snapshot (can take minutes on a large DB) ...",
        file=sys.stderr,
        flush=True,
    )


def _backup_command(args: argparse.Namespace, db_path: Path) -> int:
    with Database(db_path, migrate=False) as db:
        _announce_snapshot()
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
        "schema_version": BACKUP_ENVELOPE_VERSION,
        "backup": {
            "snapshot_path": str(snapshot_path),
            "size_bytes": size_bytes,
        },
    }


def _restore_command(args: argparse.Namespace, db_path: Path) -> int:
    src: Path = _resolve_restore_src(args, db_path)
    # migrate=False: restoring a pre-migration snapshot is how `migrate
    # --backup` is undone, so neither the live DB nor the restored copy migrates.
    with _writer_lock(db_path), Database(db_path, migrate=False) as db:
        db.restore_snapshot(src)
    if args.as_json:
        print(json.dumps(_build_restore_envelope(src)))
    else:
        print(f"restored from {src}")
    return 0


def _build_restore_envelope(src: Path) -> dict[str, object]:
    return {
        "schema_version": RESTORE_ENVELOPE_VERSION,
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


SNAPSHOTS_ENVELOPE_VERSION: int = 2


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
        "schema_version": SNAPSHOTS_ENVELOPE_VERSION,
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


def _print_summary(report: IndexReport, *, dry_run: bool = False) -> None:
    secs = report.duration_ms / 1000.0
    verb = "Would index" if dry_run else "Indexed"
    noun = "file" if report.files_indexed == 1 else "files"
    print(
        f"{verb} {report.files_indexed} {noun} "
        f"({report.files_skipped_unchanged} unchanged, "
        f"{report.files_skipped_empty} empty, "
        f"{report.files_failed} failed) "
        f"in {secs:.1f}s.",
    )
    if dry_run:
        print("Dry run: nothing was written.")
        return
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


def _build_envelope(report: IndexReport, *, status: str | None = None) -> dict[str, object]:
    return {
        "schema_version": INDEX_ENVELOPE_VERSION,
        "index": {
            "status": status or _envelope_status(report),
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


def _migrate_noting_it(db: Database) -> None:
    """Apply pending migrations, saying so on stderr.

    stderr keeps a `--json` envelope alone on stdout, and for the SessionEnd
    hook's background run it is the log, so an upgrade shows up there.
    """
    from_version = db.schema_version()

    def announce(_version: int, filename: str) -> None:
        print(
            f"convo: applying {filename} (can take minutes on a large DB) ...",
            file=sys.stderr,
            flush=True,
        )

    if db.apply_migrations(on_start=announce):
        print(f"convo: migrated schema {from_version} -> {db.schema_version()}", file=sys.stderr)


def _existing_projects_dir(explicit: Path | None) -> Path:
    projects_dir = _resolve_projects_dir(explicit)
    if not projects_dir.exists():
        msg = f"projects dir does not exist: {projects_dir}"
        raise RuntimeError(msg)
    if not projects_dir.is_dir():
        msg = f"projects dir is not a directory: {projects_dir}"
        raise RuntimeError(msg)
    return projects_dir


def _index_scope(args: argparse.Namespace, grant: str) -> str | None:
    """Return the `--session` filter to index under `grant` (None: every transcript)."""
    if grant != "after_wait":
        return cast("str | None", args.session)
    if not args.as_json:
        print("indexing every transcript, newest first: other sessions may have ended meanwhile")
    return None


def _index_command(args: argparse.Namespace, db_path: Path) -> int:
    projects_dir = _existing_projects_dir(args.projects_dir)
    with _index_lock(args, db_path) as grant:
        if grant is None:
            # A hook run already logged why it is not indexing.
            return 0 if args.hook_run else _report_index_busy(args)
        files = discover_transcripts(projects_dir, session_id=_index_scope(args, grant))
        total = len(files)
        i = 0

        def progress(result: IndexResult) -> None:
            nonlocal i
            i += 1
            if not args.as_json:
                print(_format_file_line(i, total, projects_dir, result))

        with Database(db_path, migrate=False) as db:
            if args.no_migrate:
                db.require_schema(SCHEMA_VERSION)
            elif args.dry_run:
                db.require_schema(READ_MIN_SCHEMA_VERSION)
            else:
                _migrate_noting_it(db)
            report = index_tree(
                db,
                projects_dir,
                full=args.full,
                dry_run=args.dry_run,
                on_file=progress,
                files=files,
            )

    if args.as_json:
        print(json.dumps(_build_envelope(report)))
    else:
        _print_summary(report, dry_run=args.dry_run)
    return 1 if _envelope_status(report) == "error" else 0


def _report_index_busy(args: argparse.Namespace) -> int:
    if args.as_json:
        empty = IndexReport(
            files_seen=0,
            files_indexed=0,
            files_skipped_unchanged=0,
            files_skipped_empty=0,
            files_failed=0,
            rows_inserted={},
            unknown_record_types={},
            errors=[],
            duration_ms=0,
        )
        print(json.dumps(_build_envelope(empty, status="busy")))
    else:
        print("another convo writer is running; nothing indexed (run convo index again later)")
    return 0


def _index_guard_command(args: argparse.Namespace, db_path: Path) -> int:
    log_path = resolve_guard_log_path(args.path)
    if log_path is None:
        # Explicit --path that doesn't resolve is an error (exit 1). Auto-discovery
        # finding nothing is a clean exit-0 — the user didn't ask for a specific file.
        explicit_miss = args.path is not None
        msg = f"path not found: {args.path}" if explicit_miss else "no guard JSONL log found"
        if args.as_json:
            envelope = {
                "schema_version": INDEX_ENVELOPE_VERSION,
                "guard": {"status": "no_log", "path": None, "inserted_rows": {}},
            }
            print(json.dumps(envelope))
        else:
            print(f"convo: {msg}", file=sys.stderr)
        return 1 if explicit_miss else 0
    with _writer_lock(db_path), Database(db_path) as db:
        result = index_guard_file(db, log_path, force=args.full)
    if args.as_json:
        envelope = {
            "schema_version": INDEX_ENVELOPE_VERSION,
            "guard": {
                "path": str(log_path),
                "skipped_reason": result.skipped_reason,
                "error": result.error,
                "inserted_rows": dict(result.inserted_rows),
                "source_file_id": result.source_file_id,
            },
        }
        print(json.dumps(envelope))
    else:
        if result.error is not None:
            print(f"convo: error indexing {log_path}: {result.error}", file=sys.stderr)
            return 1
        if result.skipped_reason is not None:
            print(f"{log_path}  ({result.skipped_reason})")
        else:
            inserted = result.inserted_rows.get("guard_decisions", 0)
            quarantined = result.inserted_rows.get("quarantined", 0)
            tail = f", {quarantined} quarantined" if quarantined else ""
            print(f"{log_path}  indexed {inserted} decisions{tail}")
    return 0


def _migrate_command(args: argparse.Namespace, db_path: Path) -> int:
    def announce(_version: int, filename: str) -> None:
        if not args.as_json:
            print(f"applying {filename} (can take minutes on a large DB) ...", flush=True)

    with _writer_lock(db_path), Database(db_path, migrate=False) as db:
        assert db.conn is not None
        from_version = db.schema_version()
        backup_path, backup_status = (
            _migrate_backup(db, as_json=args.as_json) if args.backup else (None, "not_requested")
        )
        applied = db.apply_migrations(on_start=announce)
        if applied:
            # Fold the migration's WAL pages back into the DB file. Best effort:
            # an active reader can leave it incomplete, which is harmless.
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        to_version = db.schema_version()

    if args.as_json:
        envelope = _build_migrate_envelope(
            from_version,
            to_version,
            applied,
            backup_path,
            backup_status,
        )
        print(json.dumps(envelope))
    elif applied:
        total_s = sum(m.duration_ms for m in applied) / 1000.0
        plural = "" if len(applied) == 1 else "s"
        print(
            f"migrated schema {from_version} -> {to_version} "
            f"({len(applied)} migration{plural}, {total_s:.1f}s)",
        )
        print("run `convo index` to catch up on sessions that changed since.")
    else:
        print(f"schema {to_version} is up to date; nothing to migrate")
    return 0


def _migrate_backup(db: Database, *, as_json: bool) -> tuple[Path | None, str]:
    """Snapshot before migrating; return (snapshot path, `backup_status`).

    `backup_status` is `written`, `up_to_date` (nothing pending, so nothing to
    protect: usually the hook's background run has already migrated) or
    `empty_db` (no indexed rows to save).
    """
    assert db.conn is not None
    if not db.pending_migrations():
        if not as_json:
            print(f"no snapshot written: already at schema {db.schema_version()}")
        return None, "up_to_date"
    if not db.conn.execute("SELECT EXISTS(SELECT 1 FROM source_files)").fetchone()[0]:
        if not as_json:
            print("no snapshot written: the DB has no indexed rows")
        return None, "empty_db"
    _announce_snapshot()
    backup_path = db.backup_snapshot()
    if not as_json:
        print(f"snapshot written: {backup_path}")
    return backup_path, "written"


def _build_migrate_envelope(
    from_version: int,
    to_version: int,
    applied: list[AppliedMigration],
    backup_path: Path | None,
    backup_status: str,
) -> dict[str, object]:
    return {
        "schema_version": MIGRATE_ENVELOPE_VERSION,
        "migrate": {
            "status": "migrated" if applied else "up_to_date",
            "from_version": from_version,
            "to_version": to_version,
            "applied": [dataclasses.asdict(m) for m in applied],
            "backup_path": None if backup_path is None else str(backup_path),
            "backup_status": backup_status,
        },
    }


def _hook_command(args: argparse.Namespace, db_path: Path) -> int:
    # A terminal on stdin means a person typed the command: don't block on read().
    stdin = sys.stdin
    text = "" if stdin is None or stdin.isatty() else stdin.read()
    run = run_pre_compact if args.event == "pre-compact" else run_session_end
    outcome = run(text, db_path=db_path)
    if args.as_json:
        print(json.dumps(_build_hook_envelope(args.event, outcome)))
    elif outcome.status == "spawned":
        print(
            f"convo: indexing session {outcome.session_id} in the background "
            f"(pid {outcome.pid}); log: {outcome.log_path}",
        )
    else:
        print("convo: no session id in the hook input; nothing started")
    return 0


def _build_hook_envelope(event: str, outcome: HookOutcome) -> dict[str, object]:
    return {
        "schema_version": HOOK_ENVELOPE_VERSION,
        "hook": {
            "event": event,
            "status": outcome.status,
            "session_id": outcome.session_id,
            "pid": outcome.pid,
            "log_path": None if outcome.log_path is None else str(outcome.log_path),
        },
    }


def _info_command(args: argparse.Namespace, db_path: Path) -> int:
    with _read_db(db_path) as db:
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
        "schema_version": INFO_ENVELOPE_VERSION,
        "info": {
            "db_schema_version": report.schema_version,
            "pending_migrations": report.pending_migrations,
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
    schema = str(report.schema_version)
    if report.pending_migrations:
        plural = "" if report.pending_migrations == 1 else "s"
        schema += f" ({report.pending_migrations} pending migration{plural}; run: convo migrate)"
    rows: list[tuple[str, str]] = [
        ("schema_version", schema),
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
    fmt = "json" if args.as_json else args.format
    project_resolved: str | None = args.project

    if args.project:
        with _read_db(db_path) as _db_proj:
            assert _db_proj.conn is not None
            try:
                project_resolved = resolve_project_path(_db_proj.conn, args.project)
            except ProjectResolveError as exc:
                if fmt == "json":
                    print(
                        json.dumps(
                            {
                                "schema_version": ERROR_ENVELOPE_VERSION,
                                "error": {"message": str(exc)},
                            }
                        )
                    )
                else:
                    print(f"convo: {exc}", file=sys.stderr)
                return 1

    with _read_db(db_path) as db:
        hits = list(
            search(
                db,
                args.query,
                since=args.since,
                project=project_resolved,
                tool=args.tool,
                session=args.session,
                tool_exact=args.tool_exact,
                excerpt_chars=args.excerpt_chars,
                limit=args.limit,
            ),
        )
        # Matches before --limit. max() covers an index run landing between the queries.
        total = max(
            len(hits),
            count_matches(
                db,
                args.query,
                since=args.since,
                project=project_resolved,
                tool=args.tool,
                session=args.session,
                tool_exact=args.tool_exact,
            ),
        )

    cleaned: list[tuple[str, list[list[int]]]] = [
        extract_indices_and_clean(h.excerpt) for h in hits
    ]

    if fmt == "json":
        suggestions = _suggestions_for(args, hits)
        filters = _filters_dict(args, project_resolved)
        envelope: dict[str, object] = {
            "schema_version": SEARCH_ENVELOPE_VERSION,
            "search": {
                "query": args.query,
                "filters": filters,
                "total": total,
                "hits": [
                    _hit_to_dict_v2(h, clean, indices)
                    for h, (clean, indices) in zip(hits, cleaned, strict=True)
                ],
            },
        }
        if not hits and suggestions:
            search_block = cast("dict[str, object]", envelope["search"])
            search_block["suggestions"] = [
                {"kind": s.kind, "description": s.description} for s in suggestions
            ]
        print(json.dumps(envelope))
    else:
        suggestions = _suggestions_for(args, hits)
        cleaned_hits = [
            dataclasses.replace(h, excerpt=clean)
            for h, (clean, _) in zip(hits, cleaned, strict=True)
        ]
        config = SearchRenderConfig(fields=args.fields or [])
        prose = render_search_hits(
            query=args.query,
            hits=cleaned_hits,
            total=total,
            config=config,
            suggestions=[(s.kind, s.description) for s in suggestions],
        )
        print(prose)

    return 0


def _hit_to_dict_v2(hit: SearchHit, clean: str, indices: list[list[int]]) -> dict[str, object]:
    d: dict[str, object] = {
        "kind": hit.kind,
        "id": hit.id,
        "session_id": hit.session_id,
        "timestamp": hit.timestamp,
        "excerpt": clean,
        "indices": indices,
        "position": hit.position,
    }
    if hit.project is not None:
        d["project"] = hit.project
    if hit.role is not None:
        d["role"] = hit.role
    if hit.tool_origin is not None:
        d["tool_origin" if hit.kind == "tool_result" else "tool"] = hit.tool_origin
    return d


def _suggestions_for(args: argparse.Namespace, hits: list[SearchHit]) -> list[Suggestion]:
    if hits:
        return []
    return generate_suggestions(
        query=args.query,
        since_span=_span_to_str(args.since),
        project=args.project,
        tool=args.tool,
        session=args.session,
        total_doc_freq={},
    )


def _filters_dict(args: argparse.Namespace, project_resolved: str | None) -> dict[str, object]:
    return {
        "since": _span_to_str(args.since),
        "project": project_resolved,
        "tool": args.tool,
        "session": args.session,
        "tool_exact": args.tool_exact,
        "limit": args.limit,
    }


def _span_to_str(span: timedelta | None) -> str | None:
    if span is None:
        return None
    total_s = int(span.total_seconds())
    for divisor, suffix in (
        (86400 * 365, "y"),
        (86400, "d"),
        (3600, "h"),
        (60, "m"),
    ):
        n, rem = divmod(total_s, divisor)
        if n > 0 and rem == 0:
            return f"{n}{suffix}"
    return f"{total_s}s"


def _strip_snippet_markers(excerpt: str) -> str:
    return excerpt.replace(SNIPPET_PRE, "").replace(SNIPPET_POST, "")


def _render_excerpt_for_tty(excerpt: str) -> str:
    if sys.stdout.isatty():
        return excerpt.replace(SNIPPET_PRE, _ANSI_BOLD_ON).replace(SNIPPET_POST, _ANSI_BOLD_OFF)
    return _strip_snippet_markers(excerpt)


def _inspect_command(args: argparse.Namespace, db_path: Path) -> int:
    with _read_db(db_path) as db:
        if args.latest:
            resolved = resolve_latest_session(db)
        else:
            resolved = resolve_session_id(db, args.session_id)
        if getattr(args, "timeline", False):
            max_chars = _TIMELINE_PREVIEW_CHARS if args.max_chars is None else args.max_chars
            events, meta = build_timeline(
                db,
                resolved,
                from_message=args.from_message,
                to_message=args.to_message,
                max_chars=max_chars,
            )
            if args.as_json:
                print(json.dumps(_build_timeline_envelope(resolved, events, meta, args)))
                return 0
            out = render_timeline(
                session_id=resolved,
                project=str(meta["project"]) if meta["project"] is not None else None,
                duration_seconds=int(meta["duration_seconds"]),
                message_count=int(meta["message_count"]),
                tool_call_count=int(meta["tool_call_count"]),
                events=events,
                from_message=args.from_message,
                to_message=args.to_message,
            )
            sys.stdout.write(out + "\n")
            return 0
        view = inspect_session(
            db,
            resolved,
            full=bool(args.full),
            from_message=args.from_message,
            to_message=args.to_message,
        )
    explicit: int | None = args.max_chars
    content_chars = _INSPECT_PREVIEW_CHARS if explicit is None else explicit
    if args.as_json:
        # JSON keeps tool input whole unless --max-chars is given.
        tool_chars = 0 if explicit is None else explicit
        print(json.dumps(_build_inspect_envelope(view, content_chars, tool_chars, args)))
    else:
        tool_chars = _INSPECT_TOOL_INPUT_PREVIEW if explicit is None else explicit
        _print_inspect(view, content_chars, tool_chars)
    return 0


def _build_timeline_envelope(
    session_id: str,
    events: list[TimelineEvent],
    meta: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, object]:
    return {
        "schema_version": INSPECT_ENVELOPE_VERSION,
        "inspect": {
            "session": _session_to_dict(
                session_id,
                started_at=meta["started_at"],
                ended_at=meta["ended_at"],
                project_path=meta["project"],
                model=meta["model"],
                git_branch=meta["git_branch"],
            ),
            "timeline": {
                "duration_seconds": meta["duration_seconds"],
                "message_count": meta["message_count"],
                "tool_call_count": meta["tool_call_count"],
                "from_message": args.from_message,
                "to_message": args.to_message,
                "events": [
                    {
                        "offset_seconds": ev.offset_seconds,
                        "role": ev.role,
                        "tool": ev.tool,
                        "preview": ev.preview,
                        "truncated": ev.truncated,
                        "position": ev.position,
                    }
                    for ev in events
                ],
            },
        },
    }


def _session_to_dict(  # noqa: PLR0913
    session_id: str,
    *,
    started_at: str | None,
    ended_at: str | None,
    project_path: str | None,
    model: str | None,
    git_branch: str | None,
) -> dict[str, object]:
    """Session header shared by the normal and ``--timeline`` JSON bodies."""
    return {
        "id": session_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "project_path": project_path,
        "model": model,
        "git_branch": git_branch,
    }


def _build_inspect_envelope(
    view: SessionView,
    content_chars: int,
    tool_chars: int,
    args: argparse.Namespace,
) -> dict[str, object]:
    return {
        "schema_version": INSPECT_ENVELOPE_VERSION,
        "inspect": {
            "session": _session_to_dict(
                view.id,
                started_at=view.started_at,
                ended_at=view.ended_at,
                project_path=view.project_path,
                model=view.model,
                git_branch=view.git_branch,
            ),
            "messages": [
                _message_to_dict(m, position, content_chars, tool_chars)
                for position, m in enumerate(view.messages, start=view.first_index)
            ],
            "truncated": view.truncated,
            "total_messages": view.total_messages,
            "from_message": args.from_message,
            "to_message": args.to_message,
        },
    }


def _message_to_dict(
    msg: MessageView, position: int, content_chars: int, tool_chars: int
) -> dict[str, object]:
    return {
        "id": msg.id,
        "position": position,
        "role": msg.role,
        "timestamp": msg.timestamp,
        "content": _truncate(msg.content, content_chars),
        "truncated": 0 < content_chars < len(msg.content),
        "tool_calls": [_tool_call_to_dict(tc, tool_chars) for tc in msg.tool_calls],
    }


def _tool_call_to_dict(tc: ToolCallView, tool_chars: int) -> dict[str, object]:
    return {
        "id": tc.id,
        "name": tc.name,
        "input_json": _truncate(tc.input_json, tool_chars),
        "truncated": 0 < tool_chars < len(tc.input_json),
        "started_at": tc.started_at,
    }


def _truncate(s: str, limit: int) -> str:
    """Clip ``s`` to ``limit`` code points plus ``...``; ``limit=0`` means no limit."""
    if limit == 0 or len(s) <= limit:
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


def _print_inspect(view: SessionView, content_chars: int, tool_chars: int) -> None:
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
    print(f"messages  ({view.total_messages} total)")
    for idx, msg in enumerate(view.messages, start=view.first_index):
        _print_message(idx, msg, content_chars, tool_chars)
    if not view.truncated:
        return
    last = view.first_index + len(view.messages) - 1
    if view.first_index > 1 or view.window_end < view.total_messages:
        print(
            f"(showing messages {view.first_index}-{last} of the selected "
            f"{view.first_index}-{view.window_end}; use --full for all)"
        )
    else:
        print(
            f"(showing {len(view.messages)} of {view.total_messages} messages; use --full for all)"
        )


def _print_message(idx: int, msg: MessageView, content_chars: int, tool_chars: int) -> None:
    icon = _role_icon(msg.role)
    ts = msg.timestamp or "(no ts)"
    content = _truncate(msg.content, content_chars)
    content = content.replace("\n", " ").replace("\r", " ")
    print(f"{idx}. {icon} {ts}  {content}")
    for tc in msg.tool_calls:
        preview = _truncate(tc.input_json.replace("\n", " "), tool_chars)
        print(f"  → {tc.name}: {preview}")


_STATS_FAMILY_FUNCS: dict[
    str,
    Callable[
        ...,
        ToolsReport | CommandsReport | SessionsReport | FilesReport | ModelReport | HooksReport,
    ],
] = {
    "tools": stats_tools,
    "commands": stats_commands,
    "sessions": stats_sessions,
    "files": stats_files,
    "model": stats_model,
    "hooks": stats_hooks,
}
# Families that count sessions and so take `include_subagents`; the rest total work.
_SESSION_COUNT_FAMILIES = frozenset({"commands", "sessions", "model"})


def _stats_command(args: argparse.Namespace, db_path: Path) -> int:
    family: str = args.family
    func = _STATS_FAMILY_FUNCS[family]
    project_resolved: str | None = None
    if args.project is not None:
        ro = _open_ro(str(db_path))
        try:
            try:
                project_resolved = resolve_project_path(
                    ro, args.project, include_subagents=args.include_subagents
                )
            except ProjectResolveError as exc:
                if args.as_json:
                    print(
                        json.dumps(
                            {
                                "schema_version": ERROR_ENVELOPE_VERSION,
                                "error": {"message": str(exc)},
                            }
                        )
                    )
                else:
                    print(f"convo: {exc}", file=sys.stderr)
                return 1
        finally:
            ro.close()
    extra: dict[str, bool] = (
        {"include_subagents": args.include_subagents} if family in _SESSION_COUNT_FAMILIES else {}
    )
    with _read_db(db_path) as db:
        report = func(db, since=args.since, project=project_resolved, **extra)
    if args.as_json:
        print(json.dumps(_build_stats_envelope(family, report)))
    else:
        _print_stats(family, report)
    return 0


def _build_stats_envelope(
    family: str,
    report: ToolsReport | CommandsReport | SessionsReport | FilesReport | ModelReport | HooksReport,
) -> dict[str, object]:
    body: dict[str, object] = {"family": family, **dataclasses.asdict(report)}
    return {"schema_version": STATS_ENVELOPE_VERSION, "stats": body}


def _print_stats(
    family: str,
    report: ToolsReport | CommandsReport | SessionsReport | FilesReport | ModelReport | HooksReport,
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
    elif family == "hooks":
        assert isinstance(report, HooksReport)
        _print_stats_hooks(report)


def _print_stats_tools(report: ToolsReport) -> None:
    print(f"total  {report.total}")
    if report.total == 0:
        print("(no data)")
        return
    print()
    _print_top_frequency(report.top_by_frequency)
    print()
    _print_error_rates(report.error_rates)


def _print_top_frequency(top: tuple[ToolFreq, ...]) -> None:
    print("top by frequency:")
    if not top:
        print("  (none)")
        return
    name_w = max(len("tool"), *(len(f.name) for f in top))
    count_w = max(len("count"), *(len(str(f.count)) for f in top))
    print(f"  {'tool'.ljust(name_w)}  {'count'.rjust(count_w)}")
    print(f"  {'-' * name_w}  {'-' * count_w}")
    for f in top:
        print(f"  {f.name.ljust(name_w)}  {str(f.count).rjust(count_w)}")


def _print_error_rates(rates: tuple[ToolErrorRate, ...]) -> None:
    print("error rates:")
    if not rates:
        print("  (none)")
        return
    rows = [(er.name, str(er.total), str(er.errors), f"{er.error_rate:.2%}") for er in rates]
    name_w = max(len("tool"), *(len(r[0]) for r in rows))
    tot_w = max(len("total"), *(len(r[1]) for r in rows))
    err_w = max(len("errors"), *(len(r[2]) for r in rows))
    rate_w = max(len("rate"), *(len(r[3]) for r in rows))
    print(
        f"  {'tool'.ljust(name_w)}  "
        f"{'total'.rjust(tot_w)}  "
        f"{'errors'.rjust(err_w)}  "
        f"{'rate'.rjust(rate_w)}",
    )
    print(f"  {'-' * name_w}  {'-' * tot_w}  {'-' * err_w}  {'-' * rate_w}")
    for name, tot, err, rate in rows:
        print(
            f"  {name.ljust(name_w)}  {tot.rjust(tot_w)}  {err.rjust(err_w)}  {rate.rjust(rate_w)}",
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


def _print_stats_hooks(report: HooksReport) -> None:
    print(f"total  {report.total}")
    if report.total == 0:
        print("(no data)")
        return
    print()
    print("top by hook:")
    if not report.top_by_hook:
        print("  (none)")
    else:
        name_w = max(len("hook_id"), *(len(h.hook_id) for h in report.top_by_hook))
        count_w = max(len("count"), *(len(str(h.count)) for h in report.top_by_hook))
        print(f"  {'hook_id'.ljust(name_w)}  {'count'.rjust(count_w)}")
        print(f"  {'-' * name_w}  {'-' * count_w}")
        for h in report.top_by_hook:
            print(f"  {h.hook_id.ljust(name_w)}  {str(h.count).rjust(count_w)}")
    print()
    print("by decision:")
    if not report.by_decision:
        print("  (none)")
        return
    dec_w = max(len("decision"), *(len(d.decision) for d in report.by_decision))
    count_w = max(len("count"), *(len(str(d.count)) for d in report.by_decision))
    print(f"  {'decision'.ljust(dec_w)}  {'count'.rjust(count_w)}")
    print(f"  {'-' * dec_w}  {'-' * count_w}")
    for d in report.by_decision:
        print(f"  {d.decision.ljust(dec_w)}  {str(d.count).rjust(count_w)}")


SUMMARY_ENVELOPE_VERSION: int = 2
_SUMMARY_TOP_LIMIT: int = 5


def _add_summary_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    summary_p = sub.add_parser(
        "summary",
        help="Compose all stats families into a single dashboard.",
        epilog=(
            "Runs each of the five stats families (tools, commands, sessions, "
            "files, model) over the same (since, project) window and prints a "
            "summary section per family. Session counts are top-level sessions "
            "only; tool-call and file totals include subagent work."
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
        help="Fuzzy match: exact path, basename, or substring (see `convo projects`).",
    )
    _add_include_subagents_arg(
        summary_p, "Count subagent sessions in the commands, sessions and model sections."
    )
    summary_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _summary_command(args: argparse.Namespace, db_path: Path) -> int:
    project_resolved: str | None = None
    if args.project is not None:
        ro = _open_ro(str(db_path))
        try:
            try:
                project_resolved = resolve_project_path(
                    ro, args.project, include_subagents=args.include_subagents
                )
            except ProjectResolveError as exc:
                if args.as_json:
                    print(
                        json.dumps(
                            {
                                "schema_version": ERROR_ENVELOPE_VERSION,
                                "error": {"message": str(exc)},
                            }
                        )
                    )
                else:
                    print(f"convo: {exc}", file=sys.stderr)
                return 1
        finally:
            ro.close()
    with _read_db(db_path) as db:
        report = gather_summary(
            db,
            since=args.since,
            project=project_resolved,
            include_subagents=args.include_subagents,
        )
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
    return {"schema_version": SUMMARY_ENVELOPE_VERSION, "summary": body}


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


DIFF_ENVELOPE_VERSION: int = 2
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
            "increases, red for decreases. Session counts are top-level sessions "
            "only; tool-call and file totals include subagent work."
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
        help="Fuzzy match: exact path, basename, or substring (see `convo projects`).",
    )
    _add_include_subagents_arg(
        diff_p,
        "Count subagent sessions in the session, command and model metrics.",
    )
    diff_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit a single JSON envelope on stdout instead of prose.",
    )


def _diff_command(args: argparse.Namespace, db_path: Path) -> int:
    span: timedelta = args.since if args.since is not None else _DIFF_DEFAULT_SPAN
    project_resolved: str | None = None
    if args.project is not None:
        ro = _open_ro(str(db_path))
        try:
            try:
                project_resolved = resolve_project_path(
                    ro, args.project, include_subagents=args.include_subagents
                )
            except ProjectResolveError as exc:
                if args.as_json:
                    print(
                        json.dumps(
                            {
                                "schema_version": ERROR_ENVELOPE_VERSION,
                                "error": {"message": str(exc)},
                            }
                        )
                    )
                else:
                    print(f"convo: {exc}", file=sys.stderr)
                return 1
        finally:
            ro.close()
    with _read_db(db_path) as db:
        report = compute_diff(
            db,
            span=span,
            project=project_resolved,
            include_subagents=args.include_subagents,
        )
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
    return {"schema_version": DIFF_ENVELOPE_VERSION, "diff": body}


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


def _add_projects_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "projects",
        help="list indexed projects",
        epilog=(
            "Lists all indexed projects with session count and last-seen timestamp. "
            "Subagent sessions are not counted unless --include-subagents is given."
        ),
    )
    _add_include_subagents_arg(p, "Count subagent sessions too.")
    p.add_argument(
        "--format",
        choices=["prose", "json"],
        default="prose",
        help="output format (default: prose)",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="alias for --format=json")


def _add_tools_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "tools",
        help="list distinct tools used",
        epilog="Lists distinct tools used across sessions with call count and last-seen timestamp.",
    )
    p.add_argument(
        "--format",
        choices=["prose", "json"],
        default="prose",
        help="output format (default: prose)",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="alias for --format=json")


def _add_sessions_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "sessions",
        help="list sessions, optionally filtered",
        epilog=(
            "Lists sessions with id, project path, start/end times, and message count. "
            "Subagent sessions (ids shaped <agent-id>.<parent-session-id>) are listed "
            "only with --include-subagents."
        ),
    )
    _add_include_subagents_arg(p, "List subagent sessions too.")
    p.add_argument(
        "--project",
        type=str,
        default=None,
        help="Fuzzy match: exact path, basename, or substring (see `convo projects`).",
    )
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help="Only include sessions newer than this span (e.g. 7d, 24h, 2w, 1y, or ISO date).",
    )
    p.add_argument(
        "--limit", type=int, default=20, help="Maximum sessions to return (default: 20)."
    )
    p.add_argument(
        "--format",
        choices=["prose", "json"],
        default="prose",
        help="output format (default: prose)",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="alias for --format=json")


def _projects_command(args: argparse.Namespace, db_path: Path) -> int:
    items: list[ProjectRow] = list_projects(str(db_path), include_subagents=args.include_subagents)
    use_json = args.as_json or args.format == "json"
    if use_json:
        print(
            json.dumps(
                {
                    "schema_version": PROJECTS_ENVELOPE_VERSION,
                    "projects": {
                        "items": [
                            {"path": r.path, "sessions": r.sessions, "last_seen": r.last_seen}
                            for r in items
                        ]
                    },
                }
            )
        )
    else:
        print(f"convo projects -- {len(items)} project{'s' if len(items) != 1 else ''}")
        if not items:
            return 0
        path_w = max(len("project"), *(len(r.path) for r in items))
        print(f"{'project'.ljust(path_w)}  {'sessions':>8}  last_seen")
        print(f"{'-' * path_w}  {'-' * 8}  {'-' * 24}")
        for r in items:
            last = r.last_seen or ""
            print(f"{r.path.ljust(path_w)}  {r.sessions:>8}  {last}")
    return 0


def _tools_command(args: argparse.Namespace, db_path: Path) -> int:
    items: list[ToolRow] = list_tools(str(db_path))
    use_json = args.as_json or args.format == "json"
    if use_json:
        print(
            json.dumps(
                {
                    "schema_version": TOOLS_ENVELOPE_VERSION,
                    "tools": {
                        "items": [
                            {"name": r.name, "calls": r.calls, "last_seen": r.last_seen}
                            for r in items
                        ]
                    },
                }
            )
        )
    else:
        print(f"convo tools -- {len(items)} tool{'s' if len(items) != 1 else ''}")
        if not items:
            return 0
        name_w = max(len("tool"), *(len(r.name) for r in items))
        print(f"{'tool'.ljust(name_w)}  {'calls':>8}  last_seen")
        print(f"{'-' * name_w}  {'-' * 8}  {'-' * 24}")
        for r in items:
            last = r.last_seen or ""
            print(f"{r.name.ljust(name_w)}  {r.calls:>8}  {last}")
    return 0


def _sessions_command(args: argparse.Namespace, db_path: Path) -> int:
    resolved_project: str | None = None
    if args.project is not None:
        ro = _open_ro(str(db_path))
        try:
            resolved_project = resolve_project_path(
                ro, args.project, include_subagents=args.include_subagents
            )
        finally:
            ro.close()

    _since: str | None = None
    if args.since is not None:
        _since = since_iso(parse_span(args.since))

    items: list[SessionRow] = list_sessions(
        str(db_path),
        project=resolved_project,
        since_iso=_since,
        limit=args.limit,
        include_subagents=args.include_subagents,
    )
    use_json = args.as_json or args.format == "json"
    if use_json:
        print(
            json.dumps(
                {
                    "schema_version": SESSIONS_ENVELOPE_VERSION,
                    "sessions": {
                        "items": [
                            {
                                "id": r.id,
                                "project_path": r.project_path,
                                "started_at": r.started_at,
                                "ended_at": r.ended_at,
                                "message_count": r.message_count,
                            }
                            for r in items
                        ]
                    },
                }
            )
        )
    else:
        print(f"convo sessions -- {len(items)} session{'s' if len(items) != 1 else ''}")
        if not items:
            return 0
        proj_w = max(len("project"), *(len(r.project_path or "") for r in items))
        print(f"{'id'.ljust(36)}  {'project'.ljust(proj_w)}  {'msgs':>5}  started_at")
        print(f"{'-' * 36}  {'-' * proj_w}  {'-' * 5}  {'-' * 24}")
        for r in items:
            proj = r.project_path or ""
            started = r.started_at or ""
            print(f"{r.id.ljust(36)}  {proj.ljust(proj_w)}  {r.message_count:>5}  {started}")
    return 0
