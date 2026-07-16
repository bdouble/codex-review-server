---
description: Cross-model code review of the current branch by Codex (optionally auto-fixing clear findings)
argument-hint: "[base-branch] [--fix] [--focus bugs|security|performance|all] [--model sol|terra|luna] [--effort xhigh|max|ultra]"
allowed-tools: mcp__plugin_codex-delegate_codex-delegate__codex_review, mcp__plugin_codex-delegate_codex-delegate__codex_review_and_fix, mcp__plugin_codex-delegate_codex-delegate__codex_status, mcp__plugin_codex-delegate_codex-delegate__codex_result, Read, Grep, Glob, Bash(git diff:*), Bash(git status:*), Bash(git log:*), Bash(git branch:*)
---

# Codex Cross-Model Review

A second model reads the same code with none of your assumptions. That is the
whole value — so do not defend the code, and do not dismiss findings because
you wrote it.

## Arguments

$ARGUMENTS

## How to run this

**1. Resolve the base branch.** Use the first positional argument if given.
Otherwise infer it (`main` by default; check `git branch` if unsure). Confirm
there are actually changes to review — `git diff <base>...HEAD --stat`. If the
diff is empty, say so and stop rather than launching a run that finds nothing.

**2. Pick the mode.**

- Default → `codex_review` (read-only, findings only).
- `--fix` → `codex_review_and_fix` (auto-fixes only clear-cut P0-P2; anything
  ambiguous comes back as a question).

Reviews are worth real reasoning budget. Default to `xhigh`; use `max` on a
large or high-stakes diff. Pass `verify_command` on `--fix` runs when the repo
has tests.

**3. Report the job id, then poll** with `codex_status(job_id, wait=True)`.

**4. Triage the findings — do not just relay them.** For each one, read the
cited code yourself. A cross-model reviewer without your context produces both
genuine catches and confident misreads, and you cannot tell which from the
prose alone. For each finding, state whether you agree, and why.

On a `--fix` run, read `verification` before the summary. If `verified` is
false, lead with that. Check `git diff` to see what actually changed rather
than trusting the narration.

**5. Present.** Group by priority (P0 → P3). Separate:

- **Fixed** — with the real diff and test results
- **Needs a decision** — Codex's question plus your recommendation
- **Disagree** — findings you assessed as wrong, with your reasoning

Then ask the user what to fix. Use `/codex:follow-up` to continue in the same
thread; it still has the whole review in context.
