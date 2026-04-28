"""Smoke test: package imports and exposes a version string."""

from __future__ import annotations

import re

import convo


def test_package_imports() -> None:
    assert convo is not None


def test_version_is_pep440_compatible() -> None:
    assert re.match(r"^\d+\.\d+\.\d+(\.dev\d+)?$", convo.__version__)
