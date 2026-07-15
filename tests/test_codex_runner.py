"""Tests for codex command construction, prompts, and error classification."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codex_runner
from codex_runner import (
    CodexAuthError,
    CodexError,
    CodexNotFoundError,
    CodexRateLimitError,
    _classify_failure,
    _phase_for_item,
    build_command,
    build_delegate_prompt,
    build_follow_up_prompt,
)


@pytest.fixture(autouse=True)
def fake_codex(monkeypatch):
    """Pin the codex binary so tests do not depend on a local install."""
    monkeypatch.setattr(codex_runner.shutil, "which", lambda _: "/usr/bin/codex")


class TestBuildCommand:
    def test_fresh_exec_shape(self):
        cmd = build_command("gpt-5.6-terra", "xhigh", "read-only", "/tmp/o.txt")
        assert cmd[:2] == ["/usr/bin/codex", "exec"]
        assert "--sandbox" in cmd and cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-terra"
        assert '-c' in cmd and 'model_reasoning_effort="xhigh"' in cmd
        assert cmd[cmd.index("-o") + 1] == "/tmp/o.txt"
        assert "--json" in cmd
        # The prompt is fed on stdin, never interpolated into argv.
        assert cmd[-1] == "-"

    def test_fresh_exec_sets_color_never(self):
        cmd = build_command("gpt-5.6-terra", "xhigh", "read-only", "/tmp/o.txt")
        assert cmd[cmd.index("--color") + 1] == "never"

    def test_resume_uses_subcommand_and_thread_id(self):
        cmd = build_command(
            "gpt-5.6-terra", "high", "read-only", "/tmp/o.txt",
            resume_thread_id="abc-123",
        )
        assert cmd[:4] == ["/usr/bin/codex", "exec", "resume", "abc-123"]

    def test_resume_omits_sandbox_flag(self):
        # `codex exec resume` rejects --sandbox outright; passing it is an
        # immediate argv error rather than a silent fallback.
        cmd = build_command(
            "gpt-5.6-terra", "high", "workspace-write", "/tmp/o.txt",
            resume_thread_id="abc-123",
        )
        assert "--sandbox" not in cmd

    def test_resume_passes_sandbox_via_config_override(self):
        cmd = build_command(
            "gpt-5.6-terra", "high", "workspace-write", "/tmp/o.txt",
            resume_thread_id="abc-123",
        )
        assert 'sandbox_mode="workspace-write"' in cmd

    def test_resume_omits_color_flag(self):
        # resume also rejects --color.
        cmd = build_command(
            "gpt-5.6-terra", "high", "read-only", "/tmp/o.txt",
            resume_thread_id="abc-123",
        )
        assert "--color" not in cmd

    def test_effort_is_toml_quoted(self):
        # -c parses its value as TOML, so a bare word would not be a string.
        cmd = build_command("gpt-5.6-sol", "ultra", "read-only", "/tmp/o.txt")
        assert 'model_reasoning_effort="ultra"' in cmd

    def test_schema_file_is_passed_when_given(self):
        cmd = build_command(
            "gpt-5.6-terra", "xhigh", "read-only", "/tmp/o.txt",
            schema_file="/tmp/s.json",
        )
        assert cmd[cmd.index("--output-schema") + 1] == "/tmp/s.json"

    def test_schema_flag_absent_when_not_given(self):
        cmd = build_command("gpt-5.6-terra", "xhigh", "read-only", "/tmp/o.txt")
        assert "--output-schema" not in cmd

    def test_missing_binary_raises(self, monkeypatch):
        monkeypatch.setattr(codex_runner.shutil, "which", lambda _: None)
        with pytest.raises(CodexNotFoundError, match="npm i -g @openai/codex"):
            build_command("gpt-5.6-terra", "xhigh", "read-only", "/tmp/o.txt")


class TestClassifyFailure:
    def test_rate_limit_phrase(self):
        with pytest.raises(CodexRateLimitError):
            _classify_failure("Error: rate limit exceeded", 1)

    def test_http_429_in_status_context(self):
        with pytest.raises(CodexRateLimitError):
            _classify_failure("request failed with status 429", 1)

    def test_bare_429_substring_is_not_a_rate_limit(self):
        # Regression: token counts and session ids contain "429" constantly.
        with pytest.raises(CodexError) as excinfo:
            _classify_failure("used 4291 tokens in session 429abc", 1)
        assert not isinstance(excinfo.value, CodexRateLimitError)

    def test_quota_exhaustion_suggests_cheaper_model(self):
        with pytest.raises(CodexRateLimitError, match="gpt-5.6-luna"):
            _classify_failure('{"error":"usage_limit_reached"}', 1)

    def test_quota_reset_time_is_surfaced(self):
        with pytest.raises(CodexRateLimitError, match="2026-07-16"):
            _classify_failure('usage_limit_reached resets_at: 2026-07-16', 1)

    def test_auth_error(self):
        with pytest.raises(CodexAuthError, match="codex login"):
            _classify_failure("401 unauthorized", 1)

    def test_bare_401_substring_is_not_an_auth_error(self):
        with pytest.raises(CodexError) as excinfo:
            _classify_failure("wrote 401 lines to file", 1)
        assert not isinstance(excinfo.value, CodexAuthError)

    def test_deprecated_model_gets_actionable_message(self):
        with pytest.raises(CodexError, match="deprecated or unavailable"):
            _classify_failure(
                "The 'gpt-5.3-codex' model is not supported when using "
                "Codex with a ChatGPT account.",
                1,
            )

    def test_unknown_failure_is_generic(self):
        with pytest.raises(CodexError, match="exit 3"):
            _classify_failure("something else broke", 3)


class TestPhaseDetection:
    @pytest.mark.parametrize("command", [
        "pytest -q", "python -m pytest tests/", "npm run test", "cargo test",
        "ruff check .", "cd /x && pytest", "yarn lint", "make check",
    ])
    def test_verification_commands(self, command):
        item = {"type": "command_execution", "command": command}
        assert _phase_for_item(item, "thinking") == "verifying"

    @pytest.mark.parametrize("command", [
        "rg --files -g '*test*'",   # regression: glob, not a test run
        "ls tests/",
        "cat build/output.txt",
        "grep -r lint src/",
        "sed -n '1,200p' calc.py",
    ])
    def test_exploration_is_not_mistaken_for_verification(self, command):
        item = {"type": "command_execution", "command": command}
        assert _phase_for_item(item, "thinking") == "investigating"

    def test_file_change_means_editing(self):
        assert _phase_for_item({"type": "file_change"}, "investigating") == "editing"

    def test_incidental_command_does_not_downgrade_editing(self):
        item = {"type": "command_execution", "command": "ls"}
        assert _phase_for_item(item, "editing") == "editing"

    def test_web_search_means_researching(self):
        assert _phase_for_item({"type": "web_search"}, "thinking") == "researching"

    def test_unknown_item_keeps_current_phase(self):
        assert _phase_for_item({"type": "mystery"}, "editing") == "editing"


class TestPrompts:
    def test_read_only_prompt_forbids_writes(self):
        prompt = build_delegate_prompt("do x", "/repo", write=False)
        assert "READ-ONLY" in prompt
        assert "do x" in prompt

    def test_write_prompt_allows_edits_but_forbids_commits(self):
        prompt = build_delegate_prompt("do x", "/repo", write=True)
        assert "may modify files" in prompt
        assert "Do NOT create git commits" in prompt

    def test_schema_prompt_drops_the_prose_contract(self):
        # The prose report contract would directly conflict with a JSON-only
        # final message.
        prompt = build_delegate_prompt("do x", "/repo", write=False, has_schema=True)
        assert "must be a single JSON object" in prompt
        assert "## Confidence" not in prompt

    def test_default_prompt_keeps_the_prose_contract(self):
        prompt = build_delegate_prompt("do x", "/repo", write=False)
        assert "## Confidence" in prompt

    def test_context_is_included_when_given(self):
        prompt = build_delegate_prompt("do x", "/repo", False, context="ticket ABC")
        assert "ticket ABC" in prompt

    def test_context_block_absent_when_empty(self):
        assert "<context>" not in build_delegate_prompt("do x", "/repo", False)

    def test_follow_up_prompt_stays_thin(self):
        # The thread already holds the original task and everything Codex
        # learned; re-sending the full preamble would just burn context.
        follow_up = build_follow_up_prompt("now do y", write=False)
        assert "now do y" in follow_up
        assert "<working_agreement>" not in follow_up
        assert len(follow_up) < len(build_delegate_prompt("now do y", "/repo", False))
