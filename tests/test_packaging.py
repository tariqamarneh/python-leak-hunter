"""Release hygiene: every place that states the version agrees."""

from __future__ import annotations

import json
import re
from pathlib import Path

import leak_hunter

ROOT = Path(__file__).parent.parent


def _pyproject_version() -> str:
    match = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_versions_agree() -> None:
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())

    assert plugin["version"] == _pyproject_version() == leak_hunter.__version__


def test_marketplace_lists_this_plugin() -> None:
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())

    assert [p["name"] for p in marketplace["plugins"]] == [plugin["name"]]
    assert (ROOT / "skills" / plugin["name"] / "SKILL.md").is_file()
