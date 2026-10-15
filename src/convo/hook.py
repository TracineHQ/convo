"""`convo hook session-end`: hand the session's indexing to a background process.

Claude Code gives SessionEnd hooks a 1.5 s budget by default (plugin hook
`timeout` values do not raise it), and re-indexing a grown transcript takes
longer than that. So the hook does no indexing itself. It reads the hook's
JSON from stdin, checks the writer lock, starts a detached
`convo index --session <id> --no-wait --no-migrate` with its output appended
to `<db>.hook.log`, and returns in milliseconds.

The background run indexes only that session's transcript, exits at once if
another writer holds the lock (that writer, or the next run, picks the file
up), and never applies migrations; after an upgrade it logs
"Run: convo migrate" until someone does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from convo.intake.orchestrator import is_valid_session_id
from convo.lock import WriterLock

if TYPE_CHECKING:
    from collections.abc import Callable

# The log keeps one or two lines per run; start over once it passes this.
LOG_MAX_BYTES: int = 1024 * 1024


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """What `convo hook session-end` did.

    `status` is `spawned` (a background index run started), `busy` (another
    writer holds the lock, so nothing started) or `skipped` (no usable
    session id in the hook input).
    """

    status: str
    session_id: str | None
    pid: int | None = None
    log_path: Path | None = None


def log_path_for(db_path: Path) -> Path:
    """Return the background run's log path for `db_path`."""
    return db_path.with_name(db_path.name + ".hook.log")


def session_id_from_payload(text: str) -> str | None:
    """Pull the session id out of Claude Code's hook JSON.

    Uses `session_id`, else the stem of `transcript_path`. Returns None for
    anything unparseable.
    """
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        return Path(transcript).stem
    return None


def child_argv(db_path: Path, session_id: str) -> list[str]:
    """Build the background index command.

    Runs this interpreter with `-P` so the working directory Claude Code
    hands the hook never lands on `sys.path`.
    """
    return [
        sys.executable,
        "-P",
        "-m",
        "convo",
        "--db",
        str(db_path),
        "index",
        "--session",
        session_id,
        "--no-wait",
        "--no-migrate",
    ]


def spawn_detached(argv: list[str], log_path: Path) -> int:
    """Start `argv` detached from the hook and return its pid.

    The child gets its own session (POSIX) or a detached process group
    (Windows), stdin from /dev/null, and stdout+stderr appended to
    `log_path`. It inherits none of the hook's pipes, so Claude Code is not
    left waiting on it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        # Create owner-only up front: the log can quote paths and errors.
        os.close(os.open(log_path, os.O_WRONLY | os.O_CREAT, 0o600))
    mode = "wb" if log_path.stat().st_size > LOG_MAX_BYTES else "ab"
    detach: dict[str, Any]
    if sys.platform == "win32":  # pragma: no cover - exercised by the Windows CI job
        detach = {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    else:
        detach = {"start_new_session": True}
    with log_path.open(mode) as log:
        proc = subprocess.Popen(  # noqa: S603 - argv is built by child_argv, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            cwd=log_path.parent,
            **detach,
        )
    return proc.pid


def run_session_end(
    stdin_text: str,
    *,
    db_path: Path,
    spawn: Callable[[list[str], Path], int] = spawn_detached,
) -> HookOutcome:
    """Handle one SessionEnd event; see the module docstring."""
    session_id = session_id_from_payload(stdin_text)
    if session_id is None or not is_valid_session_id(session_id):
        return HookOutcome(status="skipped", session_id=None)
    lock = WriterLock(db_path)
    if not lock.acquire(blocking=False):
        return HookOutcome(status="busy", session_id=session_id)
    # Only a probe: the background run takes the lock itself, and exits if
    # another writer wins it in between.
    lock.release()
    log_path = log_path_for(db_path)
    pid = spawn(child_argv(db_path, session_id), log_path)
    return HookOutcome(status="spawned", session_id=session_id, pid=pid, log_path=log_path)
