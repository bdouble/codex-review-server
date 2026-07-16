---
description: Cancel a running Codex job and stop its process
argument-hint: "<job-id>"
allowed-tools: mcp__plugin_codex-delegate_codex-delegate__codex_cancel, mcp__plugin_codex-delegate_codex-delegate__codex_status, Bash(git status:*), Bash(git diff:*)
---

# Cancel a Codex Job

## Arguments

$ARGUMENTS

## How to run this

Call `codex_cancel(job_id)`. This signals the whole process tree, so Codex stops
rather than continuing to burn quota in the background.

If no job id was given, call `codex_status()` first and confirm which job the
user means — never guess when cancelling.

## Afterwards

A cancelled **write** job may have left partial edits in the working tree. They
are not rolled back, and half-applied changes are worse than none.

Check `git status` and tell the user exactly what state the tree is in, so they
can keep or revert it deliberately. Do not revert anything without asking.
