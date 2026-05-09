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

if __name__ == "__main__":  # pragma: no cover
    try:
        rc = main()
    except BrokenPipeError:
        rc = 0
    except OSError as exc:
        # Windows raises OSError errno 22/232 on broken pipes, not BrokenPipeError.
        if sys.platform == "win32" and exc.errno in (22, 232):
            rc = 0
        else:
            raise
    with contextlib.suppress(BrokenPipeError, OSError):
        sys.stdout.flush()
    with contextlib.suppress(OSError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    sys.exit(rc)
