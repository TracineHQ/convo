# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 TracineHQ contributors
"""Golden-schema fence: a fresh DB's schema must match a committed snapshot.

The 0003 repair migration existed because an already-applied migration (0002)
was edited so the schema it produced no longer matched what its recorded version
implied -- and nothing noticed the divergence until ``convo index-guard`` failed
in production. This test builds a fresh DB from the full migration chain, dumps
the user-authored schema objects, and compares to ``tests/golden/schema.sql``.
Any change to the resulting schema that is not a proper new migration (or any
edit to an already-applied migration) makes the dump diverge and fails here.

To intentionally update the snapshot after adding a migration:
    CONVO_UPDATE_GOLDEN=1 uv run pytest tests/test_schema_golden.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from convo.db import Database

if TYPE_CHECKING:
    import sqlite3

_GOLDEN = Path(__file__).parent / "golden" / "schema.sql"

# User-authored objects only: skip sqlite_* internals and FTS5 shadow tables
# (``*_fts_data`` / ``_idx`` / ``_docsize`` / ``_config`` / ``_content``), whose
# layout is SQLite-version-dependent. The ``CREATE VIRTUAL TABLE ... fts5``
# declarations (names ending ``_fts``, no trailing shadow suffix) are kept.
_SCHEMA_QUERY = (
    "SELECT type, name, sql FROM sqlite_master "
    "WHERE sql IS NOT NULL "
    "AND name NOT LIKE 'sqlite_%' "
    r"AND name NOT LIKE '%\_fts\_%' ESCAPE '\' "
    "ORDER BY type, name, sql"
)


def _dump_schema(conn: sqlite3.Connection) -> str:
    rows = conn.execute(_SCHEMA_QUERY).fetchall()
    blocks = [f"-- {row[0]} {row[1]}\n{row[2].strip()};" for row in rows]
    return "\n\n".join(blocks) + "\n"


def test_fresh_schema_matches_golden(db_path: Path) -> None:
    with Database(db_path) as db:
        assert db.conn is not None
        dump = _dump_schema(db.conn)

    if os.environ.get("CONVO_UPDATE_GOLDEN"):
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(dump, encoding="utf-8")
        return

    assert _GOLDEN.exists(), (
        f"golden schema snapshot missing at {_GOLDEN}. Generate it with "
        "CONVO_UPDATE_GOLDEN=1 uv run pytest tests/test_schema_golden.py"
    )
    expected = _GOLDEN.read_text(encoding="utf-8")
    assert dump == expected, (
        "fresh-DB schema diverged from tests/golden/schema.sql. If you added a "
        "migration on purpose, regenerate with CONVO_UPDATE_GOLDEN=1. If you "
        "edited an ALREADY-APPLIED migration, don't -- add a new forward "
        "migration instead (see 0003_guard_decisions_repair.sql)."
    )
