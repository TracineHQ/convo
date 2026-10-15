"""Advisory single-writer lock kept beside the DB as `<db>.ingest.lock`.

convo's writers (`index`, `index-guard`, `migrate`, `restore`, and the
SessionEnd hook's background run) take it so they never interleave. It is an
OS-level lock on an open file (`flock` on POSIX, `msvcrt.locking` on
Windows), so the OS drops it when the holder exits, crashed or not; a stale
lock file on disk is harmless. `<db>.ingest.waiter.lock` works the same way
and limits the hook's background runs to one waiting for the writer lock.
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

if sys.platform == "win32":  # pragma: no cover - exercised by the Windows CI job
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


_POLL_SECONDS = 0.1


def lock_path_for(db_path: Path) -> Path:
    """Return the writer-lock path for `db_path`."""
    return db_path.with_name(db_path.name + ".ingest.lock")


class _FileLock:
    """Exclusive advisory lock on the file at `path`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self, *, blocking: bool, timeout: float | None = None) -> bool:
        """Take the lock. Without `blocking`, return False at once if it is held.

        With `blocking`, wait until it frees up, or return False once
        `timeout` seconds pass (None waits indefinitely).
        """
        if self._fd is not None:
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while not _try_lock(fd):
                if not blocking or (deadline is not None and time.monotonic() >= deadline):
                    os.close(fd)
                    return False
                time.sleep(_POLL_SECONDS)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        """Drop the lock if held. Safe to call more than once."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        finally:
            os.close(fd)


class WriterLock(_FileLock):
    """Exclusive advisory lock shared by every convo writer for one DB."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(lock_path_for(db_path))


class WaiterLock(_FileLock):
    """The single "next in line" slot for the SessionEnd hook's background runs.

    A hook run that finds the writer lock held takes this before waiting, so
    at most one run waits at a time; a run that finds it taken exits, and the
    waiter indexes its session along with every other transcript.
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path.with_name(db_path.name + ".ingest.waiter.lock"))
