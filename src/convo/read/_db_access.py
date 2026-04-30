"""Shared read-only sqlite3 connection helper for read-side modules."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_ro(path: Path | str) -> sqlite3.Connection:
    """Open a read-only sqlite3 connection to `path` via `mode=ro` URI.

    Returned connection has `row_factory = sqlite3.Row`. Caller owns close.
    """
    uri = f"{Path(path).as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
