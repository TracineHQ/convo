"""Tests for resolve_db_path precedence: explicit > $CONVO_DB > default."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from convo.db import DEFAULT_DB_PATH, resolve_db_path

if TYPE_CHECKING:
    import pytest


def test_explicit_path_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVO_DB", "/env/wins.db")
    assert resolve_db_path(Path("/nonexistent/x.db")) == Path("/nonexistent/x.db")


def test_explicit_str_arg_coerced_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVO_DB", raising=False)
    result = resolve_db_path("/nonexistent/y.db")
    assert isinstance(result, Path)
    assert result == Path("/nonexistent/y.db")


def test_env_var_used_without_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVO_DB", "/env/path.db")
    assert resolve_db_path() == Path("/env/path.db")


def test_default_when_neither_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVO_DB", raising=False)
    assert resolve_db_path() == DEFAULT_DB_PATH


def test_env_tilde_is_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVO_DB", "~/foo.db")
    result = resolve_db_path()
    assert "~" not in str(result)
    assert result.is_absolute()
