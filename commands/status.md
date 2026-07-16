---
description: Check Codex job progress, or list all delegated jobs
argument-hint: "[job-id] [--wait] [--all]"
allowed-tools: mcp__plugin_codex-delegate_codex-delegate__codex_status
---

# Codex Job Status

## Arguments

$ARGUMENTS

## How to run this

- **No argument, or `--all`** → `codex_status()` with no job id. Lists every
  job, newest first. Present as a compact table: job id, kind, status, phase,
  model, elapsed.
- **A job id (full or unique prefix)** → `codex_status(job_id)`.
- **`--wait`** → `codex_status(job_id, wait=True)`, which blocks until the job
  reaches a terminal state.

`phase` narrates what Codex is doing right now: `starting`, `thinking`,
`investigating`, `researching`, `editing`, `verifying`, `done`.

Summarize the state in a sentence rather than dumping raw JSON. If the job is
finished, collect it with `/codex:result` instead of making the user ask twice.
