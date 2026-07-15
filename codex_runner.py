"""Codex CLI subprocess management.

Spawns `codex exec` inside the target repository so Codex has the same
repo-aware capability as a manual run. Uses ChatGPT subscription auth
(via `codex login`), not API keys.

Two things here are easy to get wrong and are handled explicitly:

1. `codex exec` and `codex exec resume` do NOT share a flag set. `resume`
   rejects `--sandbox`, `--color`, and `-C/--cd`. Sandbox on a resume must go
   through `-c sandbox_mode=...` instead. Verified against codex-cli 0.144.4.

2. The prompt is fed via stdin (with `-` as the positional) rather than
   interpolated into an argv string. Task text routinely contains backticks and
   $() sequences; building a shell string around it is a command-injection bug
   waiting to happen. We never invoke a shell for codex at all.
"""

import json
import os
import re
import shutil
import subprocess
import threading

import models
from config import Config, subprocess_env


class CodexError(Exception):
    """Raised when Codex CLI execution fails."""


class CodexRateLimitError(CodexError):
    """Raised when Codex returns a rate limit error."""


class CodexNotFoundError(CodexError):
    """Raised when Codex CLI is not installed."""


class CodexAuthError(CodexError):
    """Raised when Codex CLI authentication has failed or expired."""


def find_codex_binary() -> str:
    """Find the codex CLI binary."""
    path = shutil.which("codex")
    if path is None:
        raise CodexNotFoundError(
            "Codex CLI not found. Install with: npm i -g @openai/codex\n"
            "Then authenticate with: codex login"
        )
    return path


def _build_env() -> dict:
    env = subprocess_env()
    env["CODEX_HOME"] = Config.CODEX_HOME
    return env


def _classify_failure(stderr: str, exit_code: int) -> None:
    """Raise a typed error for a known failure pattern.

    Only called when codex exited non-zero. stderr carries unrelated noise on
    successful runs too (MCP servers in the user's config log auth errors
    there), so pattern-matching it unconditionally would produce false alarms.
    """
    lowered = stderr.lower()

    # Match 429 only in an HTTP-status context, not as a substring of token
    # counts, session ids, or line numbers.
    is_rate_limit = (
        "rate limit" in lowered
        or "rate_limit" in lowered
        or "usage_limit_reached" in lowered
        or "insufficient_quota" in lowered
        or re.search(
            r"(?:http|status|code)\s+429\b|429\s+too\s+many\s+requests", lowered
        ) is not None
    )
    if is_rate_limit:
        # Quota exhaustion and transient throttling need different responses:
        # one needs a different model or a wait until reset, the other a retry.
        exhausted = (
            "usage_limit_reached" in lowered or "insufficient_quota" in lowered
        )
        reset_hint = ""
        match = re.search(r"resets?[_ ]at[\"']?\s*[:=]\s*[\"']?([^\"',}\s]+)", lowered)
        if match:
            reset_hint = f" Quota resets at {match.group(1)}."
        raise CodexRateLimitError(
            ("Codex quota exhausted." if exhausted else "Codex rate limited.")
            + reset_hint
            + " Wait and retry, or delegate to a cheaper model "
              "(e.g. gpt-5.6-luna) or a lower effort."
        )

    is_auth_error = (
        re.search(r"(?:http|status|code)\s+401\b|401\s+unauthorized", lowered) is not None
        or "unauthorized" in lowered
        or "authentication failed" in lowered
        or "please login" in lowered
        or "please log in" in lowered
        or "codex login" in lowered
    )
    if is_auth_error:
        raise CodexAuthError(
            "Codex CLI authentication failed. Your session may have expired.\n"
            "Fix: run `codex login` to re-authenticate with your ChatGPT account."
        )

    if "not supported when using codex with a chatgpt account" in lowered:
        raise CodexError(
            f"Codex rejected the model. This usually means the slug is "
            f"deprecated or unavailable on your plan.\n{stderr.strip()[:500]}"
        )

    raise CodexError(f"Codex CLI failed (exit {exit_code}): {stderr.strip()[:2000]}")


def build_command(
    model: str,
    effort: str,
    sandbox: str,
    output_file: str,
    schema_file: str | None = None,
    resume_thread_id: str | None = None,
) -> list[str]:
    """Build the codex argv.

    `exec` and `exec resume` accept different flags; see the module docstring.
    """
    codex_bin = find_codex_binary()

    if resume_thread_id:
        cmd = [codex_bin, "exec", "resume", resume_thread_id]
        # resume has no --sandbox flag; the config override is the only route.
        cmd += ["-c", f'sandbox_mode="{sandbox}"']
    else:
        cmd = [codex_bin, "exec"]
        cmd += ["--sandbox", sandbox]
        # resume rejects --color, so it is only ever set on a fresh exec.
        cmd += ["--color", "never"]

    cmd += ["--model", model]
    cmd += ["-c", f'model_reasoning_effort="{effort}"']
    cmd += ["--skip-git-repo-check", "--json"]
    cmd += ["-o", output_file]

    if schema_file:
        cmd += ["--output-schema", schema_file]

    # Read the prompt from stdin rather than argv.
    cmd.append("-")
    return cmd


# Test/lint/build runners, matched as invocations rather than as bare words.
_VERIFY_COMMAND_PATTERN = (
    r"(?:^|[;&|]\s*|\s)(?:pytest|jest|vitest|ruff|eslint|tsc|mypy|rspec|phpunit)\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|lint|build|typecheck)\b"
    r"|\b(?:make|cargo|go|dotnet|mvn|gradle)\s+(?:test|build|check|lint)\b"
    r"|\bpython\s+-m\s+(?:pytest|unittest|mypy)\b"
)


def _phase_for_item(item: dict, current: str) -> str:
    """Map a Codex event item to a coarse progress phase."""
    item_type = item.get("type")

    if item_type == "file_change":
        return "editing"
    if item_type == "command_execution":
        command = str(item.get("command", ""))
        # Match real test/lint invocations only. A bare \btest\b would fire on
        # things like `rg --files -g '*test*'`, which is exploration, not
        # verification — and mislabelling that had the phase jump to
        # "verifying" three seconds into a read-only run.
        if re.search(_VERIFY_COMMAND_PATTERN, command, re.IGNORECASE):
            return "verifying"
        # Don't downgrade editing back to investigating on an incidental command.
        return current if current in ("editing", "verifying") else "investigating"
    if item_type in ("web_search", "mcp_tool_call"):
        return "researching"
    if item_type == "reasoning":
        return current if current != "starting" else "thinking"
    return current


def run_codex(
    project_dir: str,
    prompt: str,
    model: str,
    effort: str,
    sandbox: str,
    output_file: str,
    prompt_file: str,
    stderr_file: str,
    timeout: int,
    schema_file: str | None = None,
    resume_thread_id: str | None = None,
    on_event=None,
    on_spawn=None,
) -> dict:
    """Run codex, streaming its JSONL events.

    Returns {thread_id, usage, output, structured_output, timed_out, exit_code}.
    Raises the typed Codex errors on a recognized failure.

    on_spawn(pid) is called as soon as codex starts, so the caller can record
    the pid and reap it later if this process dies without cleaning up.
    """
    cmd = build_command(
        model=model,
        effort=effort,
        sandbox=sandbox,
        output_file=output_file,
        schema_file=schema_file,
        resume_thread_id=resume_thread_id,
    )

    with open(prompt_file, "w") as handle:
        handle.write(prompt)

    state = {"thread_id": None, "usage": None, "phase": "starting", "timed_out": False,
             "turn_error": None}

    # stderr goes to a file rather than a second pipe: draining only stdout
    # while stderr fills its pipe buffer would deadlock on a chatty run.
    with open(prompt_file, "rb") as stdin_handle, open(stderr_file, "w") as err_handle:
        proc = subprocess.Popen(
            cmd,
            cwd=project_dir,
            env=_build_env(),
            stdin=stdin_handle,
            stdout=subprocess.PIPE,
            stderr=err_handle,
            text=True,
        )

        if on_spawn:
            on_spawn(proc.pid)

        def _kill():
            state["timed_out"] = True
            try:
                proc.kill()
            except OSError:
                pass

        watchdog = threading.Timer(timeout, _kill)
        watchdog.start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                if event_type == "thread.started":
                    state["thread_id"] = event.get("thread_id")
                elif event_type == "turn.completed":
                    state["usage"] = event.get("usage")
                elif event_type == "turn.failed":
                    state["turn_error"] = json.dumps(event.get("error", {}))[:500]
                elif event_type == "item.completed":
                    item = event.get("item", {})
                    state["phase"] = _phase_for_item(item, state["phase"])

                if on_event:
                    on_event(event, state)
            proc.wait()
        finally:
            watchdog.cancel()
            if proc.stdout:
                proc.stdout.close()
            # If we leave this block by an exception — on_event raising because
            # a job write failed, say — codex is still running, and would carry
            # on editing files and burning quota with nobody reading its output.
            # Never leave it behind.
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

    output = ""
    if os.path.exists(output_file):
        with open(output_file) as handle:
            output = handle.read()

    stderr = ""
    if os.path.exists(stderr_file):
        with open(stderr_file) as handle:
            stderr = handle.read()

    if state["timed_out"]:
        # Always report a timeout as a timeout. Salvage partial work when there
        # is any — a long task that produced a usable answer before the
        # deadline is still worth reading — but an empty one is the same event
        # and must not be reported as a generic failure just because nothing
        # happened to be written yet.
        return {
            "thread_id": state["thread_id"],
            "usage": state["usage"],
            "output": output or (
                f"Codex timed out after {timeout}s with no output. "
                f"Increase CODEX_TIMEOUT, lower the effort, or narrow the task."
            ),
            "structured_output": _parse_structured(output, schema_file),
            "timed_out": True,
            "exit_code": proc.returncode,
        }

    if proc.returncode != 0:
        _classify_failure(stderr or state["turn_error"] or "", proc.returncode)

    return {
        "thread_id": state["thread_id"],
        "usage": state["usage"],
        "output": output,
        "structured_output": _parse_structured(output, schema_file),
        "timed_out": False,
        "exit_code": proc.returncode,
    }


def _parse_structured(output: str, schema_file: str | None) -> dict | None:
    """Parse the final message as JSON when a schema was requested."""
    if not schema_file or not output.strip():
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_FOLLOW_THROUGH = """<follow_through>
Keep working until the task is genuinely complete. Do not stop at the first
plausible answer, and do not hand back a plan when the task asked for the work
itself. If an approach fails, try another before giving up.

If the task is ambiguous, choose the most reasonable interpretation, state it
explicitly, and proceed. You are running non-interactively — nobody can answer
a clarifying question mid-run.
</follow_through>"""

_GROUNDING = """<grounding>
Ground every claim in files you actually opened. Never describe code you have
not read, and never invent a file path, function, or test result. Cite the
files you relied on.
</grounding>"""

_VERIFICATION = """<verification>
Verify your own work before finishing:
- Re-read every region you changed and confirm it is correct in context.
- If a test suite exists, run the relevant tests and report the real output.
- Report only results you actually observed. "Should work" is not verification.
- If you could not verify something, say so plainly and explain why.
</verification>"""

_REPORT_CONTRACT = """<output_contract>
End with exactly this structure:

## Summary
What you did, in 2-3 sentences.

## Changes
Every file you modified and why. Write "None" if you changed nothing.

## Verification
Commands you ran and their actual results. Write "None run" if you ran none.

## Confidence
high / medium / low — and what would raise it.

## Open questions
Anything that needs a human decision. Write "None" if there are none.
</output_contract>"""


def _sandbox_block(write: bool) -> str:
    if write:
        return """<constraints>
You may modify files in this workspace.
- Make the minimal change the task requires. Touch nothing unrelated.
- Match the surrounding code's existing style and conventions.
- Do NOT create git commits unless the task explicitly asks — leave changes in
  the working tree so they can be reviewed.
- Do NOT amend history, force-push, or touch the git remote.
</constraints>"""
    return """<constraints>
This is a READ-ONLY task. Do not modify, create, or delete any file. The
sandbox enforces this — attempting a write will fail and waste the run.
Investigate and report; do not try to fix anything.
</constraints>"""


def build_delegate_prompt(
    task: str,
    project_dir: str,
    write: bool,
    context: str = "",
    has_schema: bool = False,
) -> str:
    """Build the prompt for an arbitrary delegated task."""
    context_block = f"\n<context>\n{context}\n</context>\n" if context else ""

    # With --output-schema, Codex's final message must be JSON matching the
    # schema, so the prose report contract would directly conflict with it.
    closing = (
        """<output_contract>
Your final message must be a single JSON object matching the provided schema.
No prose, no markdown fences — just the JSON.
</output_contract>"""
        if has_schema
        else _REPORT_CONTRACT
    )

    return f"""<task>
{task}
</task>
{context_block}
<working_agreement>
You are a delegated agent working in {project_dir}. You have full read access
to this repository — explore it before acting. Prefer reading the real code
over assuming how it works.
</working_agreement>

{_sandbox_block(write)}

{_FOLLOW_THROUGH}

{_GROUNDING}

{_VERIFICATION}

{closing}"""


def build_follow_up_prompt(task: str, write: bool) -> str:
    """Build a follow-up prompt for an existing thread.

    The thread already carries the original task, constraints, and everything
    Codex learned — so this stays deliberately thin. Re-sending the full
    preamble would just burn context re-establishing what it already knows.
    """
    return f"""<follow_up>
{task}
</follow_up>

{_sandbox_block(write)}

{_VERIFICATION}

{_REPORT_CONTRACT}"""


def _review_preamble(base_branch: str, focus: str, context: str) -> str:
    focus_line = ""
    if focus and focus != "all":
        focus_line = f"\nPay special attention to {focus} issues."
    context_block = f"\n## Additional Context\n{context}" if context else ""
    return f"""Run `git diff {base_branch}...HEAD --stat` to see what changed, then
`git diff {base_branch}...HEAD` for the actual changes.

For each changed file, read the FULL file to understand context — not just the
diff hunks. Explore related files, imports, callers, and tests to understand
impact.{focus_line}{context_block}"""


_PRIORITY_SCHEME = """- **P0**: Critical — security vulnerabilities, data loss, crashes, auth bypass
- **P1**: High — significant bugs, logic errors, race conditions, missing error handling
- **P2**: Medium — edge cases, potential issues under specific conditions, incomplete validation
- **P3**: Low — minor improvements, style suggestions, documentation gaps"""


def build_review_prompt(base_branch: str, focus: str, context: str) -> str:
    """Review-only: findings, no changes."""
    return f"""Review the code changes on the current branch compared to {base_branch}.

{_review_preamble(base_branch, focus, context)}

Produce prioritized findings:
{_PRIORITY_SCHEME}

For each finding report:
- Priority (P0/P1/P2/P3)
- Category (bug/security/performance/logic/edge-case)
- File and line number
- Description with evidence from the code
- Suggested fix
- Confidence (0-1)
- Any questions or ambiguity about the right fix approach

{_GROUNDING}

Do NOT modify any files. This is a review-only pass."""


def build_review_and_fix_prompt(base_branch: str, focus: str, context: str) -> str:
    """Review + auto-fix clear-cut P0-P2."""
    return f"""Review and fix the code changes on the current branch compared to {base_branch}.

## Step 1: Review

{_review_preamble(base_branch, focus, context)}

## Step 2: Produce Prioritized Findings

{_PRIORITY_SCHEME}

## Step 3: Auto-Fix (P0-P2 only, when clear-cut)

For each P0, P1, and P2 finding, decide:
- **If the fix is clear-cut** (you are confident, there is one obvious correct
  fix, no ambiguity): apply the fix directly. Make the minimal change needed.
  Run tests after each fix if a test suite exists.
- **If you have ANY questions or uncertainty** (multiple valid approaches,
  unclear intent, needs human context, trade-off involved): do NOT fix it.
  Report it with your specific question so the developer can decide.

NEVER auto-fix a finding if you are unsure about the right approach. When in
doubt, report and ask.

P3 findings: NEVER fix. Report only.

Do NOT create git commits — leave changes in the working tree for review.

{_GROUNDING}

## Step 4: Report

### Auto-Fixed (P0-P2, clear-cut)
For each fix applied: priority, description, file(s) changed, what you changed
and why, test results.

### Needs Human Decision (P0-P2, has questions)
For each item NOT fixed: priority, description, file and line, your specific
question or the trade-off, your recommended approach.

### For Awareness (P3)
Description, file and line, why it's low priority.

### Summary
- Total findings: N (X auto-fixed, Y need human decision, Z for awareness)
- Files modified: [list]
- Tests run: pass/fail"""


def build_fix_prompt(findings: str, context: str) -> str:
    """Targeted fix pass for approved findings."""
    guidance = f"\n## Guidance\n{context}\n" if context else ""
    return f"""Fix the following specific issues in this repository.

## Approved Findings to Fix
{findings}
{guidance}
Rules:
- Fix ONLY the issues listed above — nothing else
- Make the minimal change needed for each fix
- Read the full file and related files for context before making changes
- Run tests after each fix if a test suite exists
- If a fix would require a large refactor, explain what's needed instead of
  applying it
- One logical change per fix — do not combine unrelated changes
- Do NOT create git commits — leave changes in the working tree for review

{_GROUNDING}

After fixing, report:
- What you changed for each finding
- Files modified
- Test results"""
