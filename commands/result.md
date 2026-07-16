---
description: Collect and validate the result of a finished Codex job
argument-hint: "[job-id]"
allowed-tools: mcp__plugin_codex-delegate_codex-delegate__codex_result, mcp__plugin_codex-delegate_codex-delegate__codex_status, Read, Grep, Glob, Bash(git diff:*), Bash(git status:*), Bash(git log:*)
---

# Codex Job Result

## Arguments

$ARGUMENTS

## How to run this

Call `codex_result(job_id)`. With no argument it returns the most recent job.

**Read the `verification` block before the output.** It is the only part
grounded in the repository rather than in Codex's own account of its work:

| Signal | What it means | What to do |
|---|---|---|
| `verified: true` | No check failed | Proceed, but still read the diff |
| `verify_command: fail` | The repo's own tests/lint failed | Lead with this. The work is not done. |
| `files_changed: warn` | Write task changed nothing per git | Codex may be claiming work it didn't do — check `git diff` |
| `read_only_respected: fail` | A read-only task mutated files | Surface it; something is wrong |
| `git_tracking: skip` | Not a git repo | File claims are unverifiable — say so |

**Then reconcile.** If Codex says it changed something, confirm against
`verification.git.files_changed` and, when it matters, read the diff yourself.
Report what actually happened, not what was claimed. If they disagree, the
disagreement *is* the headline.

If `structured_output` is present, the result is a validated JSON object —
use it directly rather than re-parsing the prose.

Lead your summary with the outcome, then the changes, then anything unresolved.
For another turn on the same work, use `/codex:follow-up`.
