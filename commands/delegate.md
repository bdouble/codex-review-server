---
description: Delegate a task to Codex as a background agent — engineering, research, or writing
argument-hint: "<task> [--write] [--model sol|terra|luna] [--effort low|medium|high|xhigh|max|ultra] [--verify <cmd>] [--wait]"
allowed-tools: mcp__codex-delegate__codex_delegate, mcp__codex-delegate__codex_status, mcp__codex-delegate__codex_result, mcp__codex-delegate__codex_models, Read, Grep, Glob, Bash(git diff:*), Bash(git status:*), Bash(git log:*)
---

# Delegate to Codex

Hand a task to Codex and manage it to completion. Codex is a delegate here, not
a replacement — you own the outcome.

## Task

$ARGUMENTS

## How to run this

**1. Parse the flags.** Everything not a flag is the task text.

| Flag | Meaning |
|---|---|
| `--write` | Allow file edits (`write=True`). Without it the run is read-only. |
| `--model` | `sol` → `gpt-5.6-sol`, `terra` → `gpt-5.6-terra`, `luna` → `gpt-5.6-luna`. A full slug also works. |
| `--effort` | `low`…`xhigh`, plus `max` and `ultra` (GPT-5.6 only; `ultra` is Sol/Terra only). |
| `--verify` | Shell command to check the work, e.g. `--verify "pytest -q"`. |
| `--wait` | Block until done instead of returning the job id. |

If the user didn't specify a model or effort, don't ask — pick using the table
in the `codex-delegation` skill and say which you chose and why.

**2. Write a self-contained task.** Codex cannot ask you a clarifying question
mid-run, so ambiguity becomes a wasted 10 minutes. Before delegating, make sure
the task states what "done" means. Pass anything Codex needs but can't infer —
constraints, prior findings, acceptance criteria — via `context`.

If the request is genuinely ambiguous, resolve it with the user *first*.

**3. Launch.** Call `codex_delegate`. Default to read-only; only pass
`write=True` when the task actually requires edits.

Prefer `verify_command` whenever the repo has a test or lint command — it is
the difference between Codex reporting success and success being observed.

**4. Monitor.** Report the job id to the user immediately. Then poll with
`codex_status(job_id, wait=True)`.

**5. Validate before you believe it.** Call `codex_result` and read the
`verification` block *before* the prose. Specifically:

- `verification.verified: false` → say so plainly and explain which check failed.
- A `files_changed` warning on a `--write` task → Codex may be claiming work it
  did not do. Check `git diff` yourself before repeating its claims.
- `read_only_respected: fail` → something is wrong; surface it, don't paper over it.

Never relay Codex's summary as fact without reconciling it against the
verification block. "Codex says it fixed the bug" and "the bug is fixed" are
different claims.

**6. Report.** Lead with the outcome. Include what changed, what was verified,
and any open questions Codex raised. If it needs another turn, use
`/codex:follow-up` — never re-delegate from scratch.
