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
    """`convo --version` prints `convo <version>` and exits 0.

    Argparse's `action="version"` raises `SystemExit(0)` after writing to
    stdout. We assert both the exit status and the rendered string.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out == f"convo {convo.__version__}"
    assert out == "convo 0.1.0"
