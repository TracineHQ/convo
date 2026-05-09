"""Smoke test: package imports and exposes a version string."""

from __future__ import annotations

import re

import pytest

import convo
from convo.cli import main


def test_package_imports() -> None:
    assert convo is not None


def test_version_is_pep440_compatible() -> None:
    assert re.match(r"^\d+\.\d+\.\d+(\.dev\d+)?$", convo.__version__)


def test_cli_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """`convo --version` prints a multi-line gh-style block and exits 0."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == f"convo {convo.__version__}"
    assert lines[1].startswith("tracine-convo from ")
    assert lines[2] == "https://github.com/TracineHQ/convo"
