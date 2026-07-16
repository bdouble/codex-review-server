"""Tests for live-reloaded configuration.

Config's headline promise is that editing .env takes effect on the next tool
call without a restart. That promise is only half-kept by re-reading the file:
what the reader does with a key that has *disappeared* from it is the part that
bites, because load_dotenv only ever adds to the environment.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
import models


@pytest.fixture(autouse=True)
def env_file(tmp_path, monkeypatch):
    """Point the live-reload at a scratch .env, never the developer's own."""
    path = tmp_path / ".env"
    path.write_text("")
    monkeypatch.setattr(config, "_ENV_FILE", path)
    monkeypatch.setattr(config, "_overridden", {})
    for key in ("CODEX_MODEL", "CODEX_EFFORT", "CODEX_REVIEW_MODEL",
                "CODEX_REVIEW_REASONING", "CODEX_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    return path


class TestLiveReload:
    def test_edit_applies_without_a_restart(self, env_file):
        env_file.write_text("CODEX_MODEL=gpt-5.4\n")
        assert config.Config.MODEL == "gpt-5.4"
        env_file.write_text("CODEX_MODEL=gpt-5.5\n")
        assert config.Config.MODEL == "gpt-5.5"

    def test_removing_a_key_reverts_to_the_default(self, env_file):
        # The case the live-reload existed for and did not handle: pinned to a
        # deprecated model, every job fails 400, you comment the line out — and
        # every job still ships the old slug, because load_dotenv had copied it
        # into os.environ and nothing ever took it back out.
        env_file.write_text("CODEX_MODEL=gpt-5.3-codex\n")
        assert config.Config.MODEL == "gpt-5.3-codex"
        env_file.write_text("")
        assert config.Config.MODEL == config.DEFAULT_MODEL

    def test_removing_a_key_reverts_to_the_real_environment(self, env_file, monkeypatch):
        # ...but reverting must not eat a value the shell actually exported.
        monkeypatch.setenv("CODEX_MODEL", "gpt-5.5")
        env_file.write_text("CODEX_MODEL=gpt-5.4\n")
        assert config.Config.MODEL == "gpt-5.4"
        env_file.write_text("")
        assert config.Config.MODEL == "gpt-5.5"

    def test_env_file_wins_over_the_environment(self, env_file, monkeypatch):
        monkeypatch.setenv("CODEX_MODEL", "gpt-5.5")
        env_file.write_text("CODEX_MODEL=gpt-5.4\n")
        assert config.Config.MODEL == "gpt-5.4"

    def test_missing_env_file_falls_back_to_defaults(self, env_file):
        env_file.unlink()
        assert config.Config.MODEL == config.DEFAULT_MODEL
        assert config.Config.EFFORT == config.DEFAULT_EFFORT


class TestLegacyNames:
    def test_legacy_key_still_read(self, env_file):
        env_file.write_text("CODEX_REVIEW_MODEL=gpt-5.4\n")
        assert config.Config.MODEL == "gpt-5.4"

    def test_current_key_wins_over_legacy(self, env_file):
        env_file.write_text("CODEX_MODEL=gpt-5.5\nCODEX_REVIEW_MODEL=gpt-5.4\n")
        assert config.Config.MODEL == "gpt-5.5"

    def test_legacy_none_effort_does_not_brick_every_call(self, env_file):
        # "none" was a documented reasoning level in 1.0's .env.example. No
        # model in the current catalog offers it, so an untouched legacy .env
        # made models.validate reject every single tool call before it
        # launched anything — while the CHANGELOG promised such files kept
        # working.
        env_file.write_text("CODEX_REVIEW_REASONING=none\n")
        assert config.Config.EFFORT == "low"
        assert models.validate(config.DEFAULT_MODEL, config.Config.EFFORT) is None

    def test_legacy_none_effort_is_warned_about(self, env_file):
        env_file.write_text("CODEX_REVIEW_REASONING=none\n")
        warnings = config.Config.validate()
        assert any("no longer offered" in w for w in warnings)

    def test_a_real_effort_is_passed_through_untouched(self, env_file):
        env_file.write_text("CODEX_EFFORT=xhigh\n")
        assert config.Config.EFFORT == "xhigh"
        assert config.Config.validate() == []
