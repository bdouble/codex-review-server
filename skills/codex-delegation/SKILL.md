---
name: codex-delegation
description: "Delegate work to OpenAI Codex from Claude Code and manage it to completion. Use when the user says delegate/hand off/farm out to Codex, asks for a second model's opinion or a cross-model review, wants several tasks run in parallel, or mentions codex_delegate / GPT-5.6 / Sol / Terra / Luna. Also read this before choosing a Codex model or reasoning effort, before sending a follow-up to an existing Codex job, and before reporting what a Codex job did."
---

# Delegating to Codex

Codex is another agent you can hand work to — like a subagent, except it is a
different model family with different training and different blind spots. That
difference is the point: it is worth most where a second, independent read has
value, and worth least where you would just be paying a subprocess to do what
you can already do inline.

## When delegation is worth it

Good fits:

- **Cross-model review.** A model that didn't write the code doesn't share your
  assumptions about it.
- **Deep, self-contained investigation.** "Find every caller of X and tell me
  which are unsafe" — a long slog with a crisp definition of done.
- **Parallel fan-out.** Several independent tasks at once, each in its own job.
- **A second opinion on a hard call**, where agreement or disagreement is itself
  the signal.
- **Long grinds** you don't want occupying your own context.

Poor fits:

- Anything needing back-and-forth. Codex runs non-interactively; it cannot ask
  you a question mid-run, so ambiguity becomes a wasted 10 minutes.
- Work you can finish faster inline. A job has real startup cost.
- Tasks whose definition of done is vague. Fix the task, then delegate.

## Choosing model and effort

Don't ask the user — choose, then say what you chose and why.

| Task | Model | Effort |
|---|---|---|
| Extraction, classification, structured summaries, mechanical edits | `gpt-5.6-luna` | `low`–`medium` |
| Ordinary engineering: implement, fix, refactor, test | `gpt-5.6-terra` | `high`–`xhigh` |
| Code review | `gpt-5.6-terra` | `xhigh` |
| Ambiguous, high-value, or genuinely hard problems | `gpt-5.6-sol` | `xhigh`–`max` |
| Last-resort hard problems where you'd otherwise be stuck | `gpt-5.6-sol` | `ultra` |

Constraints that are enforced, not advisory:

- **Effort validity is per-model.** `gpt-5.6-luna` has no `ultra`. `gpt-5.5`
  and the 5.4 family top out at `xhigh` — no `max`, no `ultra`.
- **`ultra` runs four agents in parallel.** Slow and expensive. Justify it.
- **Sol is strong at low effort.** Start lower than instinct suggests; its own
  default is `low`.
- **Always use the full slug.** Bare `gpt-5.6` fails under ChatGPT auth.

Call `codex_models` for the live catalog when unsure — it reflects the account,
not this document.

## Writing the task

Codex gets one shot with no chance to ask. The task must carry its own context.

- State what "done" looks like. "Improve error handling" is not a task;
  "make every public function in api.py raise ValueError on bad input, with a
  test each" is.
- Put constraints, prior findings, and acceptance criteria in `context`.
- Name the files or areas you already know are relevant. Let it explore from
  there — don't paste code it can read itself.
- If the task is ambiguous, resolve it with the user *before* delegating.

Use `result_schema` when you want data rather than prose — Codex's final message
is then a validated JSON object, returned as `structured_output`. Far more
reliable than parsing markdown.

## Permissions

Read-only by default; `write=True` is opt-in. Delegate read-only unless the task
genuinely needs to edit. An investigation that reports back is safer, cheaper to
re-run, and leaves you the decision.

`verify_command` runs on the host with your permissions, *outside* Codex's
sandbox — `write=False` constrains Codex, not that command. It comes from you,
not the model, so Codex can't inject one; just don't pass anything you wouldn't
run yourself.

**One writer per working tree — and the server enforces it.** A writer needs the
tree exclusively; readers can share with other readers. A conflicting launch is
rejected with `repo_busy` naming the blocking job, because:

- Two writers in one tree interleave edits and corrupt each other.
- A reader alongside a writer reports the *writer's* edits as its own read-only
  violation. Verification reads global git state and cannot tell which agent —
  or which human — made a change.

So parallel jobs must target **different repositories**. On `repo_busy`, wait
(`codex_status(id, wait=True)`), cancel the blocker, or pick another repo — don't
retry in a loop.

Worktrees are deliberately *not* used to work around this: a worktree is built
from a commit, so it wouldn't contain uncommitted work (Codex would silently
edit stale code) or untracked files like `.env` and `.venv` (so `verify_command`
would fail).

The lock can't stop *you*. If you edit files while a job runs, its verification
will attribute your edits to it.

## Verification: the part that matters

**Codex saying "done" is a claim, not a result.** Treat its narration as a
hypothesis and the `verification` block as the evidence.

Always read `verification` before the prose:

- `verified: false` → lead with that, and name the failing check.
- `verified: null` → nothing could be checked (usually: not a git repo).
  Say so plainly. It is not a pass, and it is not a failure either — do
  not report it as either one.
- `files_changed: warn` on a write job → git shows nothing changed. Either it
  concluded no change was needed, or it is reporting work it didn't do. Check
  `git diff` before repeating anything.
- `read_only_respected: fail` → a read-only job mutated files. Investigate.

Pass `verify_command` whenever the repo has tests or lint (`pytest -q`,
`npm test`, `ruff check .`). It turns "should work" into an observed exit code,
and it is the single highest-value argument you can add.

When you report, reconcile the claim against the evidence. "Codex says it fixed
the bug" and "the bug is fixed" are different statements, and only one of them
is worth saying.

## Follow-ups resume, never re-spawn

Use `codex_follow_up` for anything continuing existing work. The thread still
holds every file it read and every conclusion it drew. A fresh `codex_delegate`
discards that, re-pays the context cost, and may re-derive things differently.

Start fresh only when the subject genuinely changes.

Note that `write` does not carry over between turns — "investigate" and "now fix
it" are deliberately different permissions.

## Running several jobs at once

Every tool returns a job id immediately, so fan-out is just launching several:

1. Launch each `codex_delegate` — collect the job ids.
2. Poll each with `codex_status(job_id, wait=True)`, or check them as a group
   with a bare `codex_status()`.
3. Collect each with `codex_result` and verify each independently.

Give the user the job ids as you go. A 20-minute `ultra` run with no visible id
is indistinguishable from a hang.

## When things go wrong

A failed job carries an `error_type` alongside its `error` message — branch on
that field, not on the prose, which is written for the user and will change.

| `error_type` | Meaning | Response |
|---|---|---|
| `rate_limit`, message says *quota exhausted* | Out of quota | Drop to `gpt-5.6-luna` or a lower effort, or wait for the reset in the message |
| `rate_limit`, message says *rate limited* | Transient throttle | Retry shortly |
| `auth_error` | Session expired | The user runs `codex login` — you cannot do it for them |
| `codex_not_found` | CLI not installed | `npm i -g @openai/codex` |
| `codex_error` | Everything else codex rejected | Read the message: it always carries codex's own words verbatim |
| `worker_error` | The job's own worker broke, not codex | A bug here — report the message rather than retrying |

Errors returned by the *tool call* itself, before any job exists, have an
`error` field instead:

| Symptom | Meaning | Response |
|---|---|---|
| `invalid_model` | Deprecated slug or bad effort for that model | Check `codex_models` |
| `repo_busy` | Another job holds that working tree | Wait for it, cancel it, or use a different repo — never retry in a loop |
| `store_busy` | Another launch is mid-claim on the job store | Retry once after a moment |
| `not_a_repo` | `codex_review`/`codex_review_and_fix` outside a git repo — they review a branch diff, so there is nothing to read | Point at a repository, or use `codex_delegate` for an ordinary directory |
| `unsafe_project_dir` | A task aimed at a home or system root. A write there gets every file beneath it; even a read-only one reads the whole home directory and sends what it reads upstream | Almost always a path one segment short — name the actual project directory |
| `status: timeout` | Deadline hit | Output is salvaged partial work — treat as incomplete. Narrow the task or raise `CODEX_TIMEOUT` |
| Job stuck `running` | Long `max`/`ultra` run | Check `phase`. Cancel with `codex_cancel` if genuinely wedged |

A classification is a hint about what to try, never the whole story: codex
shares its stderr with the other MCP servers in the user's config, so the typed
message always carries the raw text too. Read it before acting on the label.

Never silently substitute your own work for a Codex run that failed. If the
delegation didn't happen, say so — the user asked for a second model's view,
and quietly giving them yours misrepresents what they're reading.
