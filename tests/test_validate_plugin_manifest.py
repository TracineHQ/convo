"""Tests for `scripts/validate_plugin_manifest.py`.

The validator computes its manifest paths from `__file__`, so we stage a
fake repo in `tmp_path` (with `scripts/` and `.claude-plugin/` siblings)
and run the script via subprocess for each scenario.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path


_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
_REAL_SCRIPT = _REPO_ROOT / "scripts" / "validate_plugin_manifest.py"


def _valid_plugin() -> dict[str, Any]:
    return {
        "name": "convo",
        "version": "0.1.0",
        "description": "Index Claude Code session JSONLs into local SQLite.",
        "author": {"name": "Anthony Ledesma"},
        "homepage": "https://github.com/TracineHQ/convo",
        "repository": "https://github.com/TracineHQ/convo",
    }


def _valid_marketplace() -> dict[str, Any]:
    return {
        "name": "convo-marketplace",
        "owner": {"name": "Anthony Ledesma"},
        "plugins": [
            {
                "name": "convo",
                "source": ".",
                "description": "Index Claude Code session JSONLs into local SQLite.",
                "version": "0.1.0",
            }
        ],
    }


def _stage(
    tmp_path: Path,
    *,
    plugin: dict[str, Any] | str | None,
    marketplace: dict[str, Any] | str | None,
) -> Path:
    """Stage a fake repo in `tmp_path` and return the script path to run.

    `plugin` / `marketplace` may be a dict (serialized as JSON), a raw string
    (written as-is, for malformed-JSON tests), or None (file omitted).
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_dest = scripts_dir / "validate_plugin_manifest.py"
    shutil.copy(_REAL_SCRIPT, script_dest)

    manifest_dir = tmp_path / ".claude-plugin"
    manifest_dir.mkdir()

    if plugin is not None:
        path = manifest_dir / "plugin.json"
        if isinstance(plugin, str):
            path.write_text(plugin, encoding="utf-8")
        else:
            path.write_text(json.dumps(plugin), encoding="utf-8")

    if marketplace is not None:
        path = manifest_dir / "marketplace.json"
        if isinstance(marketplace, str):
            path.write_text(marketplace, encoding="utf-8")
        else:
            path.write_text(json.dumps(marketplace), encoding="utf-8")

    return script_dest


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — sys.executable + literal args, no shell
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_happy_path_against_repo_manifests() -> None:
    """The real on-disk manifests at the repo root must validate clean."""
    result = subprocess.run(  # noqa: S603 — sys.executable + literal args, no shell
        [sys.executable, str(_REAL_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_happy_path_staged(tmp_path: Path) -> None:
    script = _stage(tmp_path, plugin=_valid_plugin(), marketplace=_valid_marketplace())
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize("missing", ["name", "version", "description"])
def test_plugin_missing_required_field(tmp_path: Path, missing: str) -> None:
    plugin = _valid_plugin()
    del plugin[missing]
    script = _stage(tmp_path, plugin=plugin, marketplace=_valid_marketplace())
    result = _run(script)
    assert result.returncode == 1
    assert "validate_plugin_manifest:" in result.stderr
    assert "plugin.json" in result.stderr
    assert f"`{missing}`" in result.stderr


def test_plugin_bad_semver(tmp_path: Path) -> None:
    plugin = _valid_plugin()
    plugin["version"] = "1.0"
    script = _stage(tmp_path, plugin=plugin, marketplace=_valid_marketplace())
    result = _run(script)
    assert result.returncode == 1
    assert "semver" in result.stderr
    assert "'1.0'" in result.stderr


def test_plugin_malformed_json(tmp_path: Path) -> None:
    script = _stage(
        tmp_path,
        plugin="{not valid json,",
        marketplace=_valid_marketplace(),
    )
    result = _run(script)
    assert result.returncode == 1
    assert "invalid JSON" in result.stderr
    assert "plugin.json" in result.stderr


def test_missing_plugin_file(tmp_path: Path) -> None:
    script = _stage(tmp_path, plugin=None, marketplace=_valid_marketplace())
    result = _run(script)
    assert result.returncode == 1
    assert "missing manifest" in result.stderr
    assert "plugin.json" in result.stderr


def test_marketplace_cross_check_name_mismatch(tmp_path: Path) -> None:
    marketplace = _valid_marketplace()
    marketplace["plugins"][0]["name"] = "not-convo"
    script = _stage(tmp_path, plugin=_valid_plugin(), marketplace=marketplace)
    result = _run(script)
    assert result.returncode == 1
    assert "does not match" in result.stderr
    assert "'not-convo'" in result.stderr
    assert "'convo'" in result.stderr


def test_marketplace_cross_check_version_mismatch(tmp_path: Path) -> None:
    marketplace = _valid_marketplace()
    marketplace["plugins"][0]["version"] = "9.9.9"
    script = _stage(tmp_path, plugin=_valid_plugin(), marketplace=marketplace)
    result = _run(script)
    assert result.returncode == 1
    assert "does not match" in result.stderr
    assert "'9.9.9'" in result.stderr
    assert "'0.1.0'" in result.stderr


def test_marketplace_cross_check_skipped_for_remote_source(tmp_path: Path) -> None:
    """Entries with non-`.` source are not cross-checked against plugin.json."""
    marketplace = _valid_marketplace()
    marketplace["plugins"][0]["source"] = "github:other/repo"
    marketplace["plugins"][0]["name"] = "different-name"
    marketplace["plugins"][0]["version"] = "9.9.9"
    script = _stage(tmp_path, plugin=_valid_plugin(), marketplace=marketplace)
    result = _run(script)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing", ["name", "owner", "plugins"])
def test_marketplace_missing_required_field(tmp_path: Path, missing: str) -> None:
    marketplace = _valid_marketplace()
    del marketplace[missing]
    script = _stage(tmp_path, plugin=_valid_plugin(), marketplace=marketplace)
    result = _run(script)
    assert result.returncode == 1
    assert "validate_plugin_manifest:" in result.stderr
    assert "marketplace.json" in result.stderr
    assert f"`{missing}`" in result.stderr
