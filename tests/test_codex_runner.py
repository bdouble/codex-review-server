"""Tests for codex command construction, prompts, and error classification."""

import sys
import time
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
            _classify_failure("Error: rate limit exceeded", "", 1)

    def test_http_429_in_status_context(self):
        with pytest.raises(CodexRateLimitError):
            _classify_failure("request failed with status 429", "", 1)

    def test_bare_429_substring_is_not_a_rate_limit(self):
        # Regression: token counts and session ids contain "429" constantly.
        with pytest.raises(CodexError) as excinfo:
            _classify_failure("used 4291 tokens in session 429abc", "", 1)
        assert not isinstance(excinfo.value, CodexRateLimitError)

    def test_quota_exhaustion_suggests_cheaper_model(self):
        with pytest.raises(CodexRateLimitError, match="gpt-5.6-luna"):
            _classify_failure('{"error":"usage_limit_reached"}', "", 1)

    def test_quota_reset_time_is_surfaced(self):
        with pytest.raises(CodexRateLimitError, match="2026-07-16"):
            _classify_failure('usage_limit_reached resets_at: 2026-07-16', "", 1)

    def test_auth_error(self):
        with pytest.raises(CodexAuthError, match="codex login"):
            _classify_failure("401 unauthorized", "", 1)

    def test_bare_401_substring_is_not_an_auth_error(self):
        with pytest.raises(CodexError) as excinfo:
            _classify_failure("wrote 401 lines to file", "", 1)
        assert not isinstance(excinfo.value, CodexAuthError)

    def test_bare_unauthorized_word_is_not_an_auth_error(self):
        # Regression: the user's other MCP servers log their own auth failures
        # to our stderr, so a bare "unauthorized" fired on unrelated failures
        # and told the user to re-run `codex login` for a bad model slug.
        with pytest.raises(CodexError) as excinfo:
            _classify_failure("mcp server 'notes': unauthorized", "", 1)
        assert not isinstance(excinfo.value, CodexAuthError)

    def test_turn_error_is_preferred_over_stderr_noise(self):
        # stderr is almost never empty (other MCP servers log into it), so
        # reading it first threw away codex's own account of the failure.
        with pytest.raises(CodexRateLimitError):
            _classify_failure(
                "mcp server 'notes' failed to start",
                '{"code":"usage_limit_reached"}',
                1,
            )

    def test_classification_never_hides_the_raw_failure(self):
        # No pattern can tell codex's own line from a bystander's in a shared
        # stream, so a misread must still carry the evidence: this stderr looks
        # like an expired session and is really a rejected model.
        with pytest.raises(CodexAuthError, match="gpt-9-nope"):
            _classify_failure(
                "mcp server 'notes': HTTP 401 Unauthorized\n"
                "ERROR: model 'gpt-9-nope' returned 400",
                "",
                1,
            )

    def test_deprecated_model_gets_actionable_message(self):
        with pytest.raises(CodexError, match="deprecated or unavailable"):
            _classify_failure(
                "The 'gpt-5.3-codex' model is not supported when using "
                "Codex with a ChatGPT account.",
                "",
                1,
            )

    def test_unknown_failure_is_generic(self):
        with pytest.raises(CodexError, match="exit 3"):
            _classify_failure("something else broke", "", 3)

    def test_unknown_failure_surfaces_both_sources(self):
        with pytest.raises(CodexError, match="context_length_exceeded"):
            _classify_failure("noise", '{"code":"context_length_exceeded"}', 3)


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


class TestClassifierPrecedence:
    def test_unrecognised_turn_error_does_not_fall_through_to_stderr(self):
        # Regression: preferring turn_error only when it *matched* handed the
        # verdict straight back to the bystanders. Codex naming a model that
        # does not exist, plus a notion MCP server's routine token-refresh
        # 401, classified as auth_error — sending the user to `codex login`
        # for a typo, on a field _summarize now invites them to branch on.
        with pytest.raises(CodexError) as excinfo:
            _classify_failure(
                "[mcp:notion] WARN failed to refresh token: HTTP 401 Unauthorized",
                "stream error: model 'gpt-5.9-foo' does not exist",
                1,
            )
        assert not isinstance(excinfo.value, CodexAuthError)
        assert "gpt-5.9-foo" in str(excinfo.value)

    def test_unrecognised_turn_error_is_not_overridden_by_rate_limit_noise(self):
        with pytest.raises(CodexError) as excinfo:
            _classify_failure(
                "[mcp:github] WARN secondary rate limit hit, backing off 30s",
                "stream error: model 'gpt-5.9-foo' does not exist",
                1,
            )
        assert not isinstance(excinfo.value, CodexRateLimitError)

    def test_stderr_still_classifies_when_codex_reported_no_turn_error(self):
        # Not every failure produces turn.failed; stderr is all we have then.
        with pytest.raises(CodexRateLimitError):
            _classify_failure('{"error":"usage_limit_reached"}', "", 1)


class TestWatchdogEnforcesTheDeadline:
    """The timeout has to hold even when codex leaves children behind.

    Regression: `proc.kill()` reaped only codex. Anything it spawned — an MCP
    server, a shell — inherited the stdout pipe and kept its write end open, so
    the reader never saw EOF. The watchdog fired, codex died, and run_codex
    blocked on regardless: the job pinned at `running` with its deadline long
    past and CODEX_TIMEOUT guaranteeing nothing. Codex spawning children is the
    normal case, so this was not a race — it was reproducible on demand.
    """

    @staticmethod
    def _fake_codex(tmp_path, body):
        script = tmp_path / "fake_codex.sh"
        script.write_text(body)
        script.chmod(0o755)
        return str(script)

    def _run(self, tmp_path, monkeypatch, body, timeout=2):
        monkeypatch.setattr(
            codex_runner, "find_codex_binary",
            lambda: self._fake_codex(tmp_path, body),
        )
        started = time.monotonic()
        result = codex_runner.run_codex(
            project_dir=str(tmp_path), prompt="x", model="m", effort="low",
            sandbox="read-only",
            output_file=str(tmp_path / "o.txt"),
            prompt_file=str(tmp_path / "p.txt"),
            stderr_file=str(tmp_path / "e.txt"),
            timeout=timeout,
        )
        return result, time.monotonic() - started

    def test_a_child_holding_stdout_cannot_outlive_the_timeout(
        self, tmp_path, monkeypatch
    ):
        result, elapsed = self._run(tmp_path, monkeypatch, (
            "#!/bin/bash\n"
            "( sleep 60 ) &\n"                   # inherits our stdout pipe
            'echo \'{"type":"thread.started","thread_id":"t1"}\'\n'
            "sleep 60\n"
        ))
        assert result["timed_out"] is True
        assert elapsed < 20, f"blocked {elapsed:.1f}s past a 2s timeout"

    def test_a_normal_run_is_not_labelled_a_timeout(self, tmp_path, monkeypatch):
        result, elapsed = self._run(tmp_path, monkeypatch, (
            "#!/bin/bash\n"
            'echo \'{"type":"thread.started","thread_id":"t1"}\'\n'
            'echo \'{"type":"turn.completed","usage":{"tokens":5}}\'\n'
        ), timeout=30)
        assert result["timed_out"] is False
        assert result["thread_id"] == "t1"
        assert elapsed < 10

    def test_the_whole_group_is_reaped_not_just_codex(self, tmp_path, monkeypatch):
        # The orphan this module exists to prevent: a child that survives the
        # run keeps burning quota with nobody reading it.
        marker = tmp_path / "orphan_alive"
        result, _ = self._run(tmp_path, monkeypatch, (
            "#!/bin/bash\n"
            f"( sleep 5; touch {marker} ) &\n"
            'echo \'{"type":"thread.started","thread_id":"t1"}\'\n'
            "sleep 60\n"
        ))
        assert result["timed_out"] is True
        time.sleep(6)
        assert not marker.exists(), "codex's child outlived the run"


class TestUsageLimitWordingFromCodex0154:
    """The out-of-quota message codex-cli actually emits.

    Captured live from codex-cli 0.154.0:

      You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage
      to purchase more credits or try again at Sep 14th, 2026 9:26 PM.

    It contains none of the spellings the classifier used to look for — no
    "rate limit", no `usage_limit_reached`, no 429 — so a plain out-of-quota
    run was reported as an unknown failure, without the advice (drop to a
    cheaper model, or wait for the reset) that is the entire point of having
    a typed rate-limit error.
    """

    LIVE_MESSAGE = (
        "You've hit your usage limit. Visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits "
        "or try again at Sep 14th, 2026 9:26 PM."
    )

    def test_live_wording_is_a_rate_limit(self):
        with pytest.raises(CodexRateLimitError):
            _classify_failure("", self.LIVE_MESSAGE, 1)

    def test_live_wording_is_reported_as_exhaustion_not_throttling(self):
        with pytest.raises(CodexRateLimitError, match="quota exhausted"):
            _classify_failure("", self.LIVE_MESSAGE, 1)

    def test_live_wording_suggests_a_cheaper_model(self):
        with pytest.raises(CodexRateLimitError, match="gpt-5.6-luna"):
            _classify_failure("", self.LIVE_MESSAGE, 1)

    def test_prose_reset_time_is_surfaced(self):
        # "try again at <date>" is prose, not the `reset_at:` field the old
        # regex expected. Matching only the structured form dropped the one
        # piece of information that tells the user how long to wait.
        with pytest.raises(CodexRateLimitError, match=r"Sep 14th, 2026 9:26 PM"):
            _classify_failure("", self.LIVE_MESSAGE, 1)

    def test_reset_time_keeps_codex_casing(self):
        # Matched against the raw text, not a lowercased copy, so the date is
        # quoted back the way codex wrote it.
        with pytest.raises(CodexRateLimitError) as excinfo:
            _classify_failure("", self.LIVE_MESSAGE, 1)
        assert "sep 14th" not in str(excinfo.value)

    def test_structured_reset_field_still_wins(self):
        with pytest.raises(CodexRateLimitError, match="2026-07-16"):
            _classify_failure("", 'usage_limit_reached resets_at: 2026-07-16', 1)

    def test_raw_message_is_still_attached(self):
        # A classification is a hint, never a replacement for the evidence.
        with pytest.raises(CodexRateLimitError, match="chatgpt.com/codex"):
            _classify_failure("", self.LIVE_MESSAGE, 1)

    def test_a_bystander_mentioning_usage_limits_does_not_win_over_turn_error(self):
        # The precedence rule still holds: codex's own account decides.
        with pytest.raises(CodexError) as excinfo:
            _classify_failure(
                "[mcp:notes] WARN approaching usage limit",
                "stream error: model 'gpt-5.9-foo' does not exist",
                1,
            )
        assert not isinstance(excinfo.value, CodexRateLimitError)


class TestStandaloneErrorEvent:
    """Codex emits a top-level {"type":"error"} event alongside turn.failed.

    When it is the *only* structured account of a failure, the fallback used to
    be stderr — a stream shared with every MCP server in the user's codex
    config. Reading the error event keeps the verdict with codex.
    """

    @staticmethod
    def _fake_codex(tmp_path, body):
        script = tmp_path / "fake_codex.sh"
        script.write_text(body)
        script.chmod(0o755)
        return str(script)

    def _run(self, tmp_path, monkeypatch, body):
        monkeypatch.setattr(
            codex_runner, "find_codex_binary",
            lambda: self._fake_codex(tmp_path, body),
        )
        return codex_runner.run_codex(
            project_dir=str(tmp_path), prompt="x", model="m", effort="low",
            sandbox="read-only",
            output_file=str(tmp_path / "o.txt"),
            prompt_file=str(tmp_path / "p.txt"),
            stderr_file=str(tmp_path / "e.txt"),
            timeout=30,
        )

    def test_error_event_classifies_when_there_is_no_turn_failed(
        self, tmp_path, monkeypatch
    ):
        with pytest.raises(CodexRateLimitError):
            self._run(tmp_path, monkeypatch, (
                "#!/bin/bash\n"
                'echo \'{"type":"thread.started","thread_id":"t1"}\'\n'
                'echo \'{"type":"error","message":"You have hit your usage limit."}\'\n'
                'echo "[mcp:notes] HTTP 401 Unauthorized" >&2\n'
                "exit 1\n"
            ))

    def test_error_event_outranks_stderr_noise(self, tmp_path, monkeypatch):
        # Without the error event this stderr classifies as an expired session
        # and sends the user to `codex login` for an out-of-quota run.
        with pytest.raises(CodexRateLimitError) as excinfo:
            self._run(tmp_path, monkeypatch, (
                "#!/bin/bash\n"
                'echo \'{"type":"error","message":"You have hit your usage limit."}\'\n'
                'echo "[mcp:notes] HTTP 401 Unauthorized" >&2\n'
                "exit 1\n"
            ))
        assert not isinstance(excinfo.value, CodexAuthError)

    def test_turn_failed_still_outranks_the_error_event(self, tmp_path, monkeypatch):
        # Both are emitted on a normal failure; turn.failed is the richer one.
        with pytest.raises(CodexError) as excinfo:
            self._run(tmp_path, monkeypatch, (
                "#!/bin/bash\n"
                'echo \'{"type":"error","message":"usage limit"}\'\n'
                'echo \'{"type":"turn.failed","error":{"message":"context_length_exceeded"}}\'\n'
                "exit 1\n"
            ))
        assert "context_length_exceeded" in str(excinfo.value)
        assert not isinstance(excinfo.value, CodexRateLimitError)

    def test_an_error_event_on_a_successful_run_is_ignored(
        self, tmp_path, monkeypatch
    ):
        # Codex can emit a transient error and then recover. Exit 0 means
        # success, and nothing is classified.
        result = self._run(tmp_path, monkeypatch, (
            "#!/bin/bash\n"
            'echo \'{"type":"error","message":"transient usage limit blip"}\'\n'
            'echo \'{"type":"thread.started","thread_id":"t1"}\'\n'
            'echo \'{"type":"turn.completed","usage":{"input_tokens":5}}\'\n'
        ))
        assert result["thread_id"] == "t1"
        assert result["timed_out"] is False


class TestResetHintExtraction:
    """The reset time is the one thing an out-of-quota user needs.

    turn_error arrives as a JSON blob, so the prose date is wrapped in the
    punctuation that closes it. Quoting that back verbatim produced
    `Quota resets at Sep 14th, 2026 9:26 PM."}`.
    """

    def test_json_wrapper_punctuation_is_stripped(self):
        blob = (
            '{"message": "You have hit your usage limit. Visit '
            'https://chatgpt.com/codex/settings/usage to purchase more credits '
            'or try again at Sep 14th, 2026 9:26 PM."}'
        )
        assert codex_runner._reset_hint(blob) == (
            " Quota resets at Sep 14th, 2026 9:26 PM."
        )

    def test_trailing_prose_is_not_swallowed(self):
        text = "try again at 5pm. Contact support if this persists."
        assert codex_runner._reset_hint(text) == " Quota resets at 5pm."

    def test_an_abbreviated_date_keeps_its_period(self):
        # A full stop ends the sentence only when a capital follows it.
        text = 'try again at Sep. 14th, 2026."}'
        assert codex_runner._reset_hint(text) == " Quota resets at Sep. 14th, 2026."

    def test_structured_field_is_preferred(self):
        text = 'reset_at: 2026-09-14T21:26:00Z — or try again at some other time'
        assert "2026-09-14T21:26:00Z" in codex_runner._reset_hint(text)

    def test_no_reset_information_yields_nothing(self):
        assert codex_runner._reset_hint("rate limit exceeded") == ""

    def test_a_runaway_capture_is_bounded(self):
        text = "try again at " + "x" * 5000
        assert len(codex_runner._reset_hint(text)) < 200
