#!/usr/bin/env python3
"""Validate convo's Claude Code plugin manifests.

Checks `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`
for required fields and cross-consistency. Stdlib-only by design — keeps
the convo runtime free of a jsonschema dependency.

Exits 0 on success. On failure, prints `validate_plugin_manifest: <error>`
to stderr and exits 1.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_PATH = _REPO_ROOT / ".claude-plugin" / "plugin.json"
_MARKETPLACE_PATH = _REPO_ROOT / ".claude-plugin" / "marketplace.json"

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _fail(msg: str) -> NoReturn:
    print(f"validate_plugin_manifest: {msg}", file=sys.stderr)
    sys.exit(1)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        _fail(f"missing manifest: {path}")
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        _fail(f"{path}: invalid JSON ({exc.msg})")
    if not isinstance(data, dict):
        _fail(f"{path}: top-level value is not an object")
    return data


def _require_str(obj: dict[str, Any], key: str, where: str) -> str:
    if key not in obj:
        _fail(f"{where}: missing required field `{key}`")
    val = obj[key]
    if not isinstance(val, str) or not val:
        _fail(f"{where}: field `{key}` must be a non-empty string")
    return val


def _optional_str(obj: dict[str, Any], key: str, where: str) -> str | None:
    if key not in obj:
        return None
    val = obj[key]
    if not isinstance(val, str) or not val:
        _fail(f"{where}: field `{key}` must be a non-empty string when present")
    return val


def _validate_plugin(data: dict[str, Any]) -> tuple[str, str]:
    where = "plugin.json"
    name = _require_str(data, "name", where)
    version = _require_str(data, "version", where)
    if not _SEMVER_RE.match(version):
        _fail(f"{where}: `version` must match semver X.Y.Z, got {version!r}")
    _require_str(data, "description", where)

    if "author" in data:
        author = data["author"]
        if not isinstance(author, dict):
            _fail(f"{where}: `author` must be an object")
        _require_str(author, "name", f"{where}.author")

    _optional_str(data, "homepage", where)
    _optional_str(data, "repository", where)
    return name, version


def _validate_marketplace(data: dict[str, Any]) -> list[dict[str, Any]]:
    where = "marketplace.json"
    _require_str(data, "name", where)

    if "owner" not in data:
        _fail(f"{where}: missing required field `owner`")
    owner = data["owner"]
    if not isinstance(owner, dict):
        _fail(f"{where}: `owner` must be an object")
    _require_str(owner, "name", f"{where}.owner")

    if "plugins" not in data:
        _fail(f"{where}: missing required field `plugins`")
    plugins = data["plugins"]
    if not isinstance(plugins, list) or not plugins:
        _fail(f"{where}: `plugins` must be a non-empty list")

    for i, plugin in enumerate(plugins):
        loc = f"{where}.plugins[{i}]"
        if not isinstance(plugin, dict):
            _fail(f"{loc}: must be an object")
        _require_str(plugin, "name", loc)
        _require_str(plugin, "source", loc)
        _require_str(plugin, "description", loc)
        version = _require_str(plugin, "version", loc)
        if not _SEMVER_RE.match(version):
            _fail(f"{loc}: `version` must match semver X.Y.Z, got {version!r}")

    return plugins


def _cross_check(
    plugin_name: str,
    plugin_version: str,
    marketplace_plugins: list[dict[str, Any]],
) -> None:
    for i, plugin in enumerate(marketplace_plugins):
        source = plugin.get("source", "")
        if source not in (".", ""):
            continue
        loc = f"marketplace.json.plugins[{i}]"
        if plugin["name"] != plugin_name:
            _fail(
                f"{loc}: name {plugin['name']!r} does not match "
                f"plugin.json name {plugin_name!r}"
            )
        if plugin["version"] != plugin_version:
            _fail(
                f"{loc}: version {plugin['version']!r} does not match "
                f"plugin.json version {plugin_version!r}"
            )


def main() -> None:
    plugin_data = _load_json(_PLUGIN_PATH)
    marketplace_data = _load_json(_MARKETPLACE_PATH)
    plugin_name, plugin_version = _validate_plugin(plugin_data)
    marketplace_plugins = _validate_marketplace(marketplace_data)
    _cross_check(plugin_name, plugin_version, marketplace_plugins)


if __name__ == "__main__":
    main()
