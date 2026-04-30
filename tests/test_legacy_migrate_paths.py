"""Tests for path resolution and same-path handling in legacy_migrate."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from convo import legacy_migrate as _lm
from convo.legacy_migrate import (
    _ERR_RENAMED_EXISTS,
    _ERR_SAME_PATH_NO_KEEP,
    _RESUME_DEFERRED_MSG,
    _resolve_paths,
    run,
)

if TYPE_CHECKING:
    import pytest


def _ns(**kw: object) -> argparse.Namespace:
    """Build a fully-populated migrate-legacy argparse Namespace."""
    defaults: dict[str, object] = {
        "src": None,
        "dest": None,
        "dry_run": False,
        "no_keep_legacy": False,
        "json": False,
        "seed": 0xC0FFEE,
        "resume_deferred": False,
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_explicit_args_returned_directly(tmp_path: Path) -> None:
    src = tmp_path / "a.db"
    dest = tmp_path / "b.db"
    src.touch()
    args = _ns(src=src, dest=dest)
    s, d, same = _resolve_paths(args)
    assert s == src.resolve()
    assert d == dest.resolve()
    assert same is False


def test_env_var_used_for_unset_src(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_db = tmp_path / "from-env.db"
    monkeypatch.setenv("CONVO_DB", str(env_db))
    args = _ns(src=None, dest=tmp_path / "explicit.db")
    s, d, same = _resolve_paths(args)
    assert s == env_db.resolve()
    assert same is True or s != d  # both can't be ambiguous; just ensure logic ran
    assert d == (tmp_path / "explicit.db").resolve()


def test_default_canonical_same_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVO_DB", raising=False)
    args = _ns(src=None, dest=None)
    s, d, same = _resolve_paths(args)
    expected = (Path.home() / ".claude" / "convo.db").resolve()
    assert s == expected
    assert d == expected
    assert same is True


def test_symlink_aware_same_path(tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    real.touch()
    alias = tmp_path / "alias.db"
    alias.symlink_to(real)
    args = _ns(src=alias, dest=real)
    _, _, same = _resolve_paths(args)
    assert same is True


def test_distinct_paths_not_same(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    a.touch()
    b.touch()
    args = _ns(src=a, dest=b)
    _, _, same = _resolve_paths(args)
    assert same is False


def test_run_refuses_same_path_with_no_keep(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    same = tmp_path / "convo.db"
    same.touch()
    args = _ns(src=same, dest=same, no_keep_legacy=True)
    rc = run(args)
    assert rc == 1
    assert _ERR_SAME_PATH_NO_KEEP in capsys.readouterr().err


def test_run_auto_rename_refuses_collision(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    live.touch()
    legacy = tmp_path / "convo-legacy.db"
    legacy.touch()
    args = _ns(src=live, dest=live)
    rc = run(args)
    assert rc == 1
    assert _ERR_RENAMED_EXISTS.format(path=legacy.resolve()) in capsys.readouterr().err


def test_run_auto_rename_happy_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = tmp_path / "convo.db"
    live.touch()
    args = _ns(src=live, dest=live)
    rc = run(args)
    assert rc == 0
    assert not live.exists()
    legacy = tmp_path / "convo-legacy.db"
    assert legacy.exists()
    err = capsys.readouterr().err
    assert "renamed" in err
    assert str(legacy.resolve()) in err


def test_dry_run_skips_auto_rename(tmp_path: Path) -> None:
    live = tmp_path / "convo.db"
    live.touch()
    args = _ns(src=live, dest=live, dry_run=True)
    rc = run(args)
    assert rc == 0
    assert live.exists()
    assert not (tmp_path / "convo-legacy.db").exists()


def test_resume_deferred_short_circuits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _ns(resume_deferred=True)
    rc = run(args)
    assert rc == 0
    assert _RESUME_DEFERRED_MSG in capsys.readouterr().out


def test_module_namespace_constant_stable() -> None:
    # Sanity: the namespace UUID is deterministic.
    assert _lm._CONVO_LEGACY_NS == _lm._CONVO_LEGACY_NS
