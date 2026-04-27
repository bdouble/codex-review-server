"""Codex CLI subprocess management.

Handles spawning codex processes for repo-aware review and fix generation.
Uses ChatGPT subscription auth (via `codex login`), not API keys.

Key design: Codex runs IN the target repository with full file access,
not in /tmp with a diff blob. This gives Codex the same repo-aware
capabilities as running it manually.
"""

import os
import re
import subprocess
import shutil
from config import Config


class CodexError(Exception):
    """Raised when Codex CLI execution fails."""
    pass


class CodexRateLimitError(CodexError):
    """Raised when Codex returns a rate limit error."""
    pass


class CodexNotFoundError(CodexError):
    """Raised when Codex CLI is not installed."""
    pass


def find_codex_binary() -> str:
    """Find the codex CLI binary."""
    path = shutil.which("codex")
    if path is None:
        raise CodexNotFoundError(
            "Codex CLI not found. Install with: brew install codex-cli\n"
            "Then authenticate with: codex login"
        )
    return path


def _build_env() -> dict:
    """Build environment for Codex subprocess."""
    env = os.environ.copy()
    env["CODEX_HOME"] = Config.CODEX_HOME
    return env


def _check_errors(result: subprocess.CompletedProcess) -> None:
    """Check subprocess result for known error patterns."""
    if result.returncode == 0:
        return

    stderr = result.stderr.strip()
    stderr_lower = stderr.lower()

    # Rate limit: match "429" only in HTTP-status context, not as a substring
    # of token counts, session IDs, line numbers, etc.
    is_rate_limit = (
        "rate limit" in stderr_lower
        or "rate_limit" in stderr_lower
        or re.search(r'(?:http|status|code)\s+429\b|429\s+too\s+many\s+requests', stderr_lower) is not None
    )
    if is_rate_limit:
        raise CodexRateLimitError(
            "Codex rate limit reached. Wait a few minutes and retry, "
            "or continue without cross-model review."
        )

    # Auth errors: match "401" only in HTTP-status context, not as a substring
    # of token counts, session IDs, line numbers, etc.
    is_auth_error = (
        re.search(r'(?:http|status|code)\s+401\b|401\s+unauthorized', stderr_lower) is not None
        or "unauthorized" in stderr_lower
        or "authentication failed" in stderr_lower
        or "please login" in stderr_lower
        or "please log in" in stderr_lower
        or "codex login" in stderr_lower
    )
    if is_auth_error:
        raise CodexError(
            "Codex CLI authentication failed. Your session may have expired.\n"
            "Fix: run `codex login` to re-authenticate with your ChatGPT account."
        )

    raise CodexError(f"Codex CLI failed (exit {result.returncode}): {stderr}")


def _run_codex(
    project_dir: str,
    prompt: str,
    sandbox: str = "read-only",
    timeout: int | None = None,
    output_file: str | None = None,
) -> str:
    """Core subprocess runner. All public functions delegate here.

    Args:
        project_dir: Path to the project repository (Codex runs here with full access)
        prompt: The prompt/instructions for Codex
        sandbox: "read-only" for review, "workspace-write" for fixes
        timeout: Timeout in seconds
        output_file: If set, use -o to capture Codex's final message to this file

    Returns:
        Codex output text.
    """
    codex_bin = find_codex_binary()
    timeout = timeout or Config.TIMEOUT

    cmd = [
        codex_bin, "exec",
        "--model", Config.MODEL,
        "-c", f"model_reasoning_effort={Config.REASONING}",
        "--sandbox", sandbox,
        "--color", "never",
    ]

    if output_file:
        cmd.extend(["-o", output_file])

    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=project_dir,
            env=_build_env(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        # Check if output file has partial results
        if output_file and os.path.exists(output_file):
            with open(output_file) as f:
                partial = f.read()
            if partial.strip():
                return partial
        raise CodexError(
            f"Codex timed out after {timeout}s. "
            f"Try increasing CODEX_REVIEW_TIMEOUT."
        )

    _check_errors(result)

    # Prefer output file if used, fall back to stdout
    if output_file and os.path.exists(output_file):
        with open(output_file) as f:
            return f.read()

    return result.stdout


def run_review_and_fix(
    project_dir: str,
    base_branch: str = "main",
    focus: str = "",
    context: str = "",
    timeout: int | None = None,
) -> str:
    """Run a repo-aware review that auto-fixes clear P0-P2 findings.

    Single Codex session that:
    1. Reviews all changes against base_branch with full repo access
    2. Produces prioritized findings (P0-P3)
    3. Auto-fixes P0-P2 findings that are clear-cut (high confidence, obvious fix)
    4. Reports but does NOT fix P0-P2 findings where there are questions or ambiguity
    5. Reports P3+ findings for awareness only

    Args:
        project_dir: Path to the project repository
        base_branch: Branch/commit to compare against
        focus: Review focus - "bugs", "security", "performance", or "all"
        context: Additional context (ticket description, acceptance criteria)
        timeout: Timeout in seconds (defaults to Config.TIMEOUT)

    Returns:
        Codex output text with findings, fixes applied, and items for human review.
    """
    effective_focus = focus or Config.FOCUS

    focus_instruction = ""
    if effective_focus != "all":
        focus_instruction = f"\nPay special attention to {effective_focus} issues."

    context_block = ""
    if context:
        context_block = f"\n## Additional Context\n{context}"

    prompt = f"""Review and fix the code changes on the current branch compared to {base_branch}.

## Step 1: Review

Run `git diff {base_branch}...HEAD --stat` to see what changed, then `git diff {base_branch}...HEAD` for the actual changes.

For each changed file, read the FULL file to understand context — not just the diff hunks. Explore related files, imports, callers, and tests to understand impact.
{focus_instruction}{context_block}

## Step 2: Produce Prioritized Findings

Classify each finding using this priority scheme:
- **P0**: Critical — security vulnerabilities, data loss, crashes, auth bypass
- **P1**: High — significant bugs, logic errors, race conditions, missing error handling
- **P2**: Medium — edge cases, potential issues under specific conditions, incomplete validation
- **P3**: Low — minor improvements, style suggestions, documentation gaps

## Step 3: Auto-Fix (P0-P2 only, when clear-cut)

For each P0, P1, and P2 finding, decide:
- **If the fix is clear-cut** (you are confident, there is one obvious correct fix, no ambiguity): apply the fix directly. Make the minimal change needed. Run tests after each fix if a test suite exists.
- **If you have ANY questions or uncertainty** (multiple valid approaches, unclear intent, needs human context, trade-off involved): do NOT fix it. Report it with your specific question so the developer can decide.

NEVER auto-fix a finding if you are unsure about the right approach. When in doubt, report and ask.

P3 findings: NEVER fix. Report only.

## Step 4: Report

After completing your review and fixes, provide a structured summary:

### Auto-Fixed (P0-P2, clear-cut)
For each fix applied:
- Priority and description
- File(s) changed
- What you changed and why
- Test results (if tests were run)

### Needs Human Decision (P0-P2, has questions)
For each item NOT fixed:
- Priority and description
- File and line
- Your specific question or the trade-off involved
- Your recommended approach (but let the human decide)

### For Awareness (P3)
For each low-priority item:
- Description
- File and line
- Why it's low priority

### Summary
- Total findings: N (X auto-fixed, Y need human decision, Z for awareness)
- Files modified: [list]
- Tests run: pass/fail"""

    output_file = "/tmp/codex-review-output.txt"
    # Clean up any previous output
    if os.path.exists(output_file):
        os.remove(output_file)

    return _run_codex(
        project_dir=project_dir,
        prompt=prompt,
        sandbox="workspace-write",
        timeout=timeout,
        output_file=output_file,
    )


def run_review_only(
    project_dir: str,
    base_branch: str = "main",
    focus: str = "",
    context: str = "",
    timeout: int | None = None,
) -> str:
    """Run a review-only pass (no fixes). Used for the initial review or when
    you want findings without any code changes.

    Args:
        project_dir: Path to the project repository
        base_branch: Branch/commit to compare against
        focus: Review focus area
        context: Additional context
        timeout: Timeout in seconds

    Returns:
        Codex output with prioritized findings.
    """
    effective_focus = focus or Config.FOCUS

    focus_instruction = ""
    if effective_focus != "all":
        focus_instruction = f"\nPay special attention to {effective_focus} issues."

    context_block = ""
    if context:
        context_block = f"\n## Additional Context\n{context}"

    prompt = f"""Review the code changes on the current branch compared to {base_branch}.

Run `git diff {base_branch}...HEAD --stat` then `git diff {base_branch}...HEAD` for the changes.

For each changed file, read the FULL file for context. Explore related files, imports, callers, and tests.
{focus_instruction}{context_block}

Produce prioritized findings:
- **P0**: Critical — security vulnerabilities, data loss, crashes, auth bypass
- **P1**: High — significant bugs, logic errors, race conditions, missing error handling
- **P2**: Medium — edge cases, potential issues, incomplete validation
- **P3**: Low — minor improvements, suggestions, documentation gaps

For each finding report:
- Priority (P0/P1/P2/P3)
- Category (bug/security/performance/logic/edge-case)
- File and line number
- Description with evidence from the code
- Suggested fix
- Confidence (0-1)
- Any questions or ambiguity about the right fix approach

Do NOT modify any files. This is a review-only pass."""

    output_file = "/tmp/codex-review-output.txt"
    if os.path.exists(output_file):
        os.remove(output_file)

    return _run_codex(
        project_dir=project_dir,
        prompt=prompt,
        sandbox="read-only",
        timeout=timeout,
        output_file=output_file,
    )


def run_fix(
    project_dir: str,
    findings: str,
    context: str = "",
    timeout: int | None = None,
) -> str:
    """Run a targeted fix pass for specific approved findings.

    Used as the second pass after the user reviews findings from
    run_review_only and approves specific items for fixing.

    Args:
        project_dir: Path to the project repository
        findings: Description of the specific findings to fix (from human review)
        context: Additional guidance on how to approach the fixes
        timeout: Timeout in seconds

    Returns:
        Codex output describing what was fixed.
    """
    prompt = f"""Fix the following specific issues in this repository.

## Approved Findings to Fix
{findings}

## Guidance
{context}

Rules:
- Fix ONLY the issues listed above — nothing else
- Make the minimal change needed for each fix
- Read the full file and related files for context before making changes
- Run tests after each fix if a test suite exists
- If a fix would require a large refactor, explain what's needed instead of applying it
- One logical change per fix — do not combine unrelated changes

After fixing, report:
- What you changed for each finding
- Files modified
- Test results"""

    output_file = "/tmp/codex-fix-output.txt"
    if os.path.exists(output_file):
        os.remove(output_file)

    return _run_codex(
        project_dir=project_dir,
        prompt=prompt,
        sandbox="workspace-write",
        timeout=timeout,
        output_file=output_file,
    )
