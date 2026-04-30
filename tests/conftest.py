"""Shared pytest fixtures for convo tests."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from convo.db import Database
from tests.fixtures.legacy_minimal_seed import seed_legacy

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "convo.db"


@pytest.fixture
def db(db_path: Path) -> Iterator[Database]:
    with Database(db_path) as database:
        yield database


@pytest.fixture
def legacy_conn() -> Iterator[sqlite3.Connection]:
    """In-memory legacy DB seeded with edge-case rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    seed_legacy(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def legacy_db_path(tmp_path: Path) -> Path:
    """File-backed legacy DB at tmp_path/legacy.db."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    seed_legacy(conn)
    conn.close()
    return path
