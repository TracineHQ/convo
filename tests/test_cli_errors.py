"""Tests for grep-style and invented flag rejection."""

from __future__ import annotations

import subprocess

import pytest


def _run_convo(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["uv", "run", "convo", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("flag", "expected_hint"),
    [
        ("-C5", "excerpt-chars"),
        ("-A3", "inspect --timeline"),
        ("-B3", "inspect --timeline"),
        ("-E", "regex"),
        ("-i", "case"),
    ],
)
def test_grep_flags_rejected_with_hint(flag: str, expected_hint: str) -> None:
    proc = _run_convo("search", "kafka", flag)
    assert proc.returncode != 0
    assert expected_hint in proc.stderr


@pytest.mark.parametrize(
    "flag",
    ["--quiet", "--no-color", "--by-caller", "--has-newlines", "--tree"],
)
def test_invented_flags_rejected_with_hint(flag: str) -> None:
    proc = _run_convo("search", "kafka", flag)
    assert proc.returncode != 0
    err_lower = proc.stderr.lower()
    assert "unknown flag" in err_lower
