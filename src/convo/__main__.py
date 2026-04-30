"""Entry point for `python -m convo`.

Handles ``BrokenPipeError`` gracefully so piping output through ``| head`` (and
similar) does not surface "Exception ignored in sys.excepthook" warnings on
stderr after a successful command. The standard recipe redirects stdout to
``/dev/null`` before the interpreter's final flush so the late writes silently
discard.
"""

from __future__ import annotations

import contextlib
import os
import sys

from convo.cli import main

if __name__ == "__main__":
    try:
        rc = main()
    except BrokenPipeError:
        rc = 0
    with contextlib.suppress(BrokenPipeError):
        sys.stdout.flush()
    with contextlib.suppress(OSError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    sys.exit(rc)
