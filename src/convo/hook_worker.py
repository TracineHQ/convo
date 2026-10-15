"""Background half of `convo hook session-end`: index one session, stamping each log line.

`convo hook session-end` starts `python -m convo.hook_worker <db> <session-id>`
detached, with stdout and stderr appended to `<db>.hook.log`. This runs
`convo --db <db> index --session <session-id>` with every line it prints
prefixed by the UTC time and the session id, because runs for several
sessions share that log and, when they queue on the writer lock, interleave
in it.
"""

from __future__ import annotations

import io
import sys
from datetime import UTC, datetime
from typing import TextIO, override

from convo import cli
from convo.intake.orchestrator import is_valid_session_id

_USAGE = "usage: python -m convo.hook_worker <db-path> <session-id>"


class _StampedWriter(io.TextIOBase):
    """Write through to `out`, starting each line with `<UTC time> <session id> `."""

    def __init__(self, out: TextIO, session_id: str) -> None:
        self._out = out
        self._session_id = session_id
        self._at_line_start = True

    @override
    def write(self, s: str) -> int:
        for piece in s.splitlines(keepends=True):
            if self._at_line_start:
                stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                self._out.write(f"{stamp} {self._session_id} ")
            self._out.write(piece)
            self._at_line_start = piece.endswith(("\n", "\r"))
        return len(s)

    @override
    def flush(self) -> None:
        self._out.flush()


def index_argv(db_path: str, session_id: str) -> list[str]:
    """Return the `convo` argv for the session's index run.

    A plain `convo index`: it waits for the writer lock (the run is detached,
    so waiting costs the hook nothing) and applies pending migrations.
    """
    return ["--db", db_path, "index", "--session", session_id]


def main(argv: list[str] | None = None) -> int:
    """Run the session's index with stamped output; return its exit code."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or not is_valid_session_id(args[1]):  # noqa: PLR2004 - <db> <session-id>
        sys.stderr.write(_USAGE + "\n")
        return 2
    db_path, session_id = args
    out, err = sys.stdout, sys.stderr
    sys.stdout = _StampedWriter(out, session_id)
    sys.stderr = _StampedWriter(err, session_id)
    try:
        return cli.main(index_argv(db_path, session_id))
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout, sys.stderr = out, err


if __name__ == "__main__":  # pragma: no cover - run by the hook as a detached process
    raise SystemExit(main())
