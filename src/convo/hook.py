"""`convo hook session-end`: hand the session's indexing to a background process.

Claude Code gives SessionEnd hooks a 1.5 s budget by default (plugin hook
`timeout` values do not raise it), and re-indexing a grown transcript takes
longer than that. So the hook does no indexing itself. It reads the hook's
JSON from stdin, starts a detached `convo index --session <id>` (through
`convo.hook_worker`, which stamps each output line with the time and session
id) with its output appended to `<db>.hook.log`, and returns in milliseconds.

The background run indexes only that session's transcript. If another writer
holds the lock, it waits its turn (it is detached, so waiting costs the hook
nothing), so a session that ends mid-index still gets indexed. Like any
`convo index`, it applies pending migrations first, so an upgrade never
stalls indexing; readers keep working meanwhile.
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

if TYPE_CHECKING:
    from collections.abc import Callable

# The log keeps one or two lines per run; start over once it passes this.
LOG_MAX_BYTES: int = 1024 * 1024


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """What `convo hook session-end` did.

    `status` is `spawned` (a background index run started; it waits for the
    writer lock if another writer holds it) or `skipped` (no usable session
    id in the hook input).
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
    """Build the background index command (see `convo.hook_worker`).

    Runs this interpreter with `-P` so the working directory Claude Code
    hands the hook never lands on `sys.path`, and `-X utf8` so the log is
    UTF-8 on Windows too (a detached run there would use the ANSI code page).
    """
    return [
        sys.executable,
        "-P",
        "-X",
        "utf8",
        "-m",
        "convo.hook_worker",
        str(db_path),
        session_id,
    ]


def spawn_detached(argv: list[str], log_path: Path) -> int:
    """Start `argv` detached from the hook and return its pid.

    The child gets its own session (POSIX) or its own process group and a
    hidden console (Windows), stdin from /dev/null, and stdout+stderr appended to
    `log_path`. It inherits none of the hook's pipes, so Claude Code is not
    left waiting on it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        # Create owner-only up front: the log can quote paths and errors.
        os.close(os.open(log_path, os.O_WRONLY | os.O_CREAT, 0o600))
    if log_path.stat().st_size > LOG_MAX_BYTES:
        os.truncate(log_path, 0)
    detach: dict[str, Any]
    if sys.platform == "win32":
        # A hidden console, not DETACHED_PROCESS (which overrides
        # CREATE_NO_WINDOW): with no console at all, a venv's python.exe
        # launcher starts the real interpreter in a new, visible window.
        detach = {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    else:
        detach = {"start_new_session": True}
    log_fd = _open_for_append(log_path)
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv is built by child_argv, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            close_fds=True,
            cwd=log_path.parent,
            **detach,
        )
    finally:
        os.close(log_fd)
    return proc.pid


def _open_for_append(path: Path) -> int:
    """Open an existing `path` so every write, from any process, lands at its end.

    Queued runs share the log. POSIX `O_APPEND` belongs to the open file, so
    the child inherits it. On Windows append is a C runtime flag on the fd,
    which a child doesn't inherit: it gets the bare handle and its own file
    position, and the runs overwrite each other. A handle opened with append
    data access but no write data access makes the OS append every write.
    """
    if sys.platform == "win32":  # pragma: no cover - exercised by the Windows CI job
        import _winapi  # noqa: PLC0415 - Windows only
        import msvcrt  # noqa: PLC0415 - Windows only

        file_generic_write, file_write_data = 0x00120116, 0x00000002
        share_all = 0x00000001 | 0x00000002 | 0x00000004  # read, write, delete
        handle = _winapi.CreateFile(
            str(path),
            file_generic_write & ~file_write_data,
            share_all,
            _winapi.NULL,
            _winapi.OPEN_EXISTING,
            0,
            _winapi.NULL,
        )
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND)
    return os.open(path, os.O_WRONLY | os.O_APPEND)


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
    # The child runs in the log's directory; a relative path would move with it.
    db_path = db_path.resolve()
    log_path = log_path_for(db_path)
    pid = spawn(child_argv(db_path, session_id), log_path)
    return HookOutcome(status="spawned", session_id=session_id, pid=pid, log_path=log_path)
