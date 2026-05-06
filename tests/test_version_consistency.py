"""Single-source-of-truth checks for release-critical facts.

``convo.__version__`` reads from installed package metadata (the dist built
from ``pyproject.toml``). These assertions catch drift between the code-side
constant, ``pyproject.toml``, ``.claude-plugin/plugin.json``, the marketplace
plugin entry, the release workflow's PyPI URL, and README documentation of
public surface (e.g. stats families).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import convo
from convo.cli import _STATS_FAMILIES

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


def test_release_workflow_pypi_url_matches_dist_name() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    dist_name = pyproject["project"]["name"]
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text()
    expected_url = f"https://pypi.org/project/{dist_name}/"
    assert expected_url in workflow, (
        f"release.yml environment URL must reference dist name {dist_name!r}; "
        f"expected {expected_url!r} to appear in release.yml"
    )


def test_readme_documents_every_stats_family() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    for family in _STATS_FAMILIES:
        assert f"`{family}`" in readme, (
            f"stats family {family!r} is registered in convo.cli._STATS_FAMILIES "
            f"but is not documented in README.md"
        )
