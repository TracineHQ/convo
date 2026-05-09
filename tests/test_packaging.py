"""Packaging contract: migrations must ship in the built wheel."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

_DIST = Path(__file__).resolve().parent.parent / "dist"


def test_migrations_present_in_built_wheel() -> None:
    if not _DIST.exists():
        pytest.skip("no dist/ directory; run `uv build` first")
    wheels = sorted(_DIST.glob("tracine_convo-*.whl"))
    if not wheels:
        pytest.skip("no built wheel under dist/")
    newest = wheels[-1]
    with zipfile.ZipFile(newest) as zf:
        names = set(zf.namelist())
    assert "convo/migrations/0001_init.sql" in names, sorted(names)
    assert "convo/migrations/0002_guard_decisions.sql" in names, sorted(names)
