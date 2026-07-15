---
description: Continue a Codex job in its original thread, keeping everything it already learned
argument-hint: "<job-id> <follow-up task> [--write] [--verify <cmd>]"
allowed-tools: mcp__codex-delegate__codex_follow_up, mcp__codex-delegate__codex_status, mcp__codex-delegate__codex_result, Read, Grep, Glob, Bash(git diff:*), Bash(git status:*)
---

# Continue a Codex Job

## Arguments

$ARGUMENTS

## Why this and not a new delegation

Resuming keeps the thread: every file Codex read, every conclusion it drew, and
the original task all stay in context. A fresh `codex_delegate` throws that away
and pays the full context-establishing cost again — slower, more expensive, and
it will re-derive things it already knew (sometimes differently).

**Rule: any follow-up on work Codex already did goes through this command.**
Start fresh only when the subject genuinely changes.

## How to run this

**1. Resolve the job.** First argument is the job id (a unique prefix is fine).
The rest is the follow-up task. If the user didn't name a job, call
`codex_status()` and use the most recent relevant one — say which you picked.

**2. Launch** `codex_follow_up(job_id, task, write=...)`. It inherits the
original job's model, effort, and project. Override with `--model`/`--effort`
only when the follow-up is meaningfully harder or easier than the first turn.

A follow-up that fixes something needs `--write`; the original job's write
setting does not carry over, because "investigate" and "now fix it" are
different permissions.

**3. Poll, then validate** exactly as in `/codex:result` — read `verification`
before the prose, and reconcile claims against `git diff`.

## If it isn't resumable

A job that failed before Codex started a session has no `thread_id` and cannot
be resumed. Say so and start a fresh `/codex:delegate` carrying the context
forward yourself.
