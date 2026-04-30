"""Shared pytest fixtures for convo tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from convo.db import Database

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
