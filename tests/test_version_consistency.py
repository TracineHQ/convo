"""Single-source-of-truth check for the package version.

``convo.__version__`` reads from installed package metadata (the dist built
from ``pyproject.toml``). These assertions catch drift between the code-side
constant, ``pyproject.toml``, ``.claude-plugin/plugin.json``, and the
marketplace plugin entry.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import convo

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_version_matches_package() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == convo.__version__


def test_plugin_manifest_version_matches_package() -> None:
    plugin = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == convo.__version__


def test_marketplace_plugin_entry_version_matches_package() -> None:
    marketplace = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entries = [p for p in marketplace["plugins"] if p["name"] == "convo"]
    assert entries, "no plugin entry named 'convo' in marketplace.json"
    assert entries[0]["version"] == convo.__version__
