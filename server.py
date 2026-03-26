"""Codex Review MCP Server.

Exposes tools for repo-aware cross-model code review via MCP stdio transport.
Codex runs as a full agent in the target repository with complete file access —
the same as running Codex manually.

Three modes:
- codex_review_and_fix: Review + auto-fix clear P0-P2 findings, report the rest
- codex_review: Review-only, no fixes (for when you want findings first)
- codex_fix: Fix specific approved findings (second pass after human review)

Install:
    claude mcp add codex-review-server -- python3 /path/to/server.py
"""

import sys
import os
import json

# Add server directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from config import Config
from codex_runner import (
    run_review_and_fix, run_review_only, run_fix,
    CodexRateLimitError, CodexNotFoundError, CodexError,
)

server = FastMCP("codex-review-server")

# Validate config on startup
warnings = Config.validate()
for w in warnings:
    print(f"[codex-review-server] WARNING: {w}", file=sys.stderr)


def _error_response(error_type: str, message: str, **extra) -> str:
    """Standard error response format."""
    return json.dumps({"error": error_type, "message": message, **extra})


@server.tool()
def codex_review_and_fix(
    project_dir: str,
    base_branch: str = "main",
    focus: str = "",
    context: str = "",
) -> str:
    """Review code changes AND auto-fix clear P0-P2 findings in one pass.

    Codex runs as a full agent with complete repository access. It:
    1. Reviews all changes against the base branch
    2. Produces prioritized findings (P0-P3)
    3. Auto-fixes P0-P2 findings that are clear-cut (confident, obvious fix)
    4. Reports P0-P2 findings it has questions about (does NOT fix these)
    5. Reports P3 findings for awareness only

    Args:
        project_dir: Absolute path to the project repository
        base_branch: Branch or commit to compare against (default: "main")
        focus: Review focus - "bugs", "security", "performance", or "all"
        context: Additional context (ticket description, acceptance criteria)

    Returns:
        Codex output with: auto-fixed items, items needing human decision, awareness items
    """
    try:
        output = run_review_and_fix(
            project_dir=project_dir,
            base_branch=base_branch,
            focus=focus,
            context=context,
        )
        return json.dumps({
            "status": "complete",
            "output": output,
            "model": Config.MODEL,
            "reasoning_effort": Config.REASONING,
        }, indent=2)
    except CodexRateLimitError as e:
        return _error_response("rate_limit", str(e))
    except CodexNotFoundError as e:
        return _error_response("codex_not_found", str(e))
    except CodexError as e:
        return _error_response("codex_error", str(e))


@server.tool()
def codex_review(
    project_dir: str,
    base_branch: str = "main",
    focus: str = "",
    context: str = "",
) -> str:
    """Review-only pass — find and report issues without fixing anything.

    Use this when you want to see all findings before any fixes are applied,
    or when you want full control over what gets fixed.

    Codex runs with read-only access. It reviews all changes, reads related
    files for context, and produces prioritized findings (P0-P3).

    Args:
        project_dir: Absolute path to the project repository
        base_branch: Branch or commit to compare against (default: "main")
        focus: Review focus - "bugs", "security", "performance", or "all"
        context: Additional context (ticket description, acceptance criteria)

    Returns:
        Prioritized findings with severity, evidence, and suggested fixes
    """
    try:
        output = run_review_only(
            project_dir=project_dir,
            base_branch=base_branch,
            focus=focus,
            context=context,
        )
        return json.dumps({
            "status": "complete",
            "output": output,
            "model": Config.MODEL,
            "reasoning_effort": Config.REASONING,
        }, indent=2)
    except CodexRateLimitError as e:
        return _error_response("rate_limit", str(e))
    except CodexNotFoundError as e:
        return _error_response("codex_not_found", str(e))
    except CodexError as e:
        return _error_response("codex_error", str(e))


@server.tool()
def codex_fix(
    project_dir: str,
    findings: str,
    context: str = "",
) -> str:
    """Fix specific approved findings (second pass after human review).

    Use this after codex_review to fix findings the user has approved.
    Codex runs with write access and makes targeted fixes.

    Args:
        project_dir: Absolute path to the project repository
        findings: The specific findings to fix (from codex_review output, filtered by user)
        context: Additional guidance on approach, constraints, or preferences

    Returns:
        Description of changes made, files modified, and test results
    """
    try:
        output = run_fix(
            project_dir=project_dir,
            findings=findings,
            context=context,
        )
        return json.dumps({
            "status": "complete",
            "output": output,
            "model": Config.MODEL,
            "reasoning_effort": Config.REASONING,
        }, indent=2)
    except CodexRateLimitError as e:
        return _error_response("rate_limit", str(e))
    except CodexNotFoundError as e:
        return _error_response("codex_not_found", str(e))
    except CodexError as e:
        return _error_response("codex_error", str(e))


if __name__ == "__main__":
    server.run(transport="stdio")
