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


def _plugin_manifest() -> dict:
    return json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())


def _mcp_server_name() -> str:
    names = list(_plugin_manifest()["mcpServers"])
    assert len(names) == 1
    return names[0]


def _mcp_tool_prefix() -> str:
    """The namespace Claude Code gives the tools of a plugin-provided server.

    Not `mcp__<server>__` — that is the shape for a user- or project-scoped
    server. One declared in plugin.json is namespaced by plugin *and* server
    key: `mcp__plugin_<plugin>_<server>__<tool>`. Deriving it from the server
    key alone is what put a prefix in every allowed-tools list that matches no
    live tool, and a test deriving it the same wrong way found nothing to
    check and passed — confirming the bug rather than catching it.
    """
    return f"mcp__plugin_{_plugin_manifest()['name']}_{_mcp_server_name()}__"


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

    def test_no_root_mcp_json(self):
        # A .mcp.json at the repo root is Claude Code's *project-scope* config
        # convention. Since this plugin's root is also a repo people open,
        # Claude Code would load it as a project server — a context where
        # ${CLAUDE_PLUGIN_ROOT} is undefined, so the command resolves to a
        # literal path and the server dies with ENOENT for everyone who opens
        # the repo. The server config belongs inline in plugin.json instead.
        assert not (ROOT / ".mcp.json").exists(), (
            "Root .mcp.json is loaded as project config, where "
            "${CLAUDE_PLUGIN_ROOT} does not expand. Keep mcpServers inline in "
            ".claude-plugin/plugin.json."
        )

    def test_mcp_server_points_at_an_executable_launcher(self):
        command = _plugin_manifest()["mcpServers"][_mcp_server_name()]["command"]
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
        prefix = _mcp_tool_prefix()
        referenced = {
            tool.strip().removeprefix(prefix)
            for tool in re.findall(rf"{re.escape(prefix)}\w+", allowed)
        }
        unknown = referenced - _tool_names()
        assert not unknown, f"{path.name} references non-existent tools: {unknown}"

    def test_mcp_tools_use_the_plugin_scoped_prefix(self, path):
        # Checking the suffix is not enough on its own: a wrong prefix matches
        # nothing, so the test above sees an empty set and passes while the
        # allow-list authorizes zero tools and every command prompts anyway.
        allowed = _frontmatter(path).get("allowed-tools", "")
        prefix = _mcp_tool_prefix()
        wrong = [
            tool
            for tool in re.findall(r"mcp__[\w-]+__\w+", allowed)
            if not tool.startswith(prefix)
        ]
        assert not wrong, (
            f"{path.name} names MCP tools that no live tool matches: {wrong}. "
            f"Plugin-provided servers are namespaced '{prefix}<tool>'."
        )


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
