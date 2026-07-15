"""Tests for the plugin packaging.

These exist because a broken command frontmatter fails silently: Claude Code
drops the metadata and loads the command with no description and, worse, no
allowed-tools restriction. Nothing errors — the guard rails just quietly
disappear. A YAML parse check is the only thing that catches it.
"""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

yaml = pytest.importorskip("yaml", reason="pyyaml is dev-only")

COMMANDS = sorted((ROOT / "commands").glob("*.md"))
FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)


def _frontmatter(path: Path) -> dict:
    match = FRONTMATTER.match(path.read_text())
    assert match, f"{path.name} has no YAML frontmatter block"
    return yaml.safe_load(match.group(1))


def _tool_names() -> set[str]:
    """Tool names as registered in server.py."""
    source = (ROOT / "server.py").read_text()
    return set(re.findall(r"@server\.tool\(\)\ndef (\w+)", source))


def _mcp_server_name() -> str:
    config = json.loads((ROOT / ".mcp.json").read_text())
    names = list(config["mcpServers"])
    assert len(names) == 1
    return names[0]


class TestManifests:
    def test_plugin_json_is_valid(self):
        manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        assert manifest["name"] == "codex-delegate"
        assert re.match(r"^\d+\.\d+\.\d+$", manifest["version"])
        assert manifest["description"]

    def test_marketplace_json_is_valid(self):
        market = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text()
        )
        assert market["plugins"][0]["source"] == "./"
        assert market["plugins"][0]["name"] == "codex-delegate"

    def test_versions_agree_across_manifests(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text()
        )
        assert plugin["version"] == market["plugins"][0]["version"]

    def test_changelog_documents_current_version(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        changelog = (ROOT / "CHANGELOG.md").read_text()
        assert f"[{plugin['version']}]" in changelog

    def test_declared_license_file_exists(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        if plugin.get("license"):
            assert (ROOT / "LICENSE").exists()

    def test_mcp_json_points_at_an_executable_launcher(self):
        config = json.loads((ROOT / ".mcp.json").read_text())
        command = config["mcpServers"][_mcp_server_name()]["command"]
        assert command.startswith("${CLAUDE_PLUGIN_ROOT}/")
        relative = command.replace("${CLAUDE_PLUGIN_ROOT}/", "")
        launcher = ROOT / relative
        assert launcher.exists(), f"{relative} is missing"
        import os
        assert os.access(launcher, os.X_OK), f"{relative} is not executable"


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: p.name)
class TestCommandFrontmatter:
    def test_frontmatter_parses(self, path):
        # Regression: `argument-hint: [job-id] [--wait]` is not valid YAML —
        # `[` opens a flow sequence and the trailing token is a parse error.
        # The whole block is then dropped, taking allowed-tools with it.
        assert isinstance(_frontmatter(path), dict)

    def test_has_description(self, path):
        assert _frontmatter(path).get("description")

    def test_argument_hint_is_a_string(self, path):
        # `[job-id]` parses as a list rather than erroring — quote it.
        hint = _frontmatter(path).get("argument-hint")
        if hint is not None:
            assert isinstance(hint, str), f"argument-hint parsed as {type(hint).__name__}"

    def test_mcp_tools_referenced_actually_exist(self, path):
        allowed = _frontmatter(path).get("allowed-tools", "")
        prefix = f"mcp__{_mcp_server_name()}__"
        referenced = {
            tool.strip().removeprefix(prefix)
            for tool in re.findall(rf"{re.escape(prefix)}\w+", allowed)
        }
        unknown = referenced - _tool_names()
        assert not unknown, f"{path.name} references non-existent tools: {unknown}"


class TestSkill:
    def test_skill_frontmatter(self):
        skill = ROOT / "skills" / "codex-delegation" / "SKILL.md"
        assert skill.exists()
        data = _frontmatter(skill)
        assert data["name"] == "codex-delegation"
        assert data["description"]

    def test_skill_description_carries_trigger_terms(self):
        skill = ROOT / "skills" / "codex-delegation" / "SKILL.md"
        description = _frontmatter(skill)["description"].lower()
        for term in ("delegate", "codex", "review"):
            assert term in description
