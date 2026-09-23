# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.3.0] — 2026-09-23

Support for **GPT-6 Sol** and **GPT-6 Luna**, verified against **codex-cli
0.156.1**, and a preference for Daybreak on accounts that have it.

### Added

- **GPT-6 Sol (`gpt-6-sol`)** — the workhorse for coding and everyday work,
  every effort up to `ultra`. **GPT-6 Luna (`gpt-6-luna`)** — fast and
  affordable, up to `max` (no `ultra`). Both need codex-cli 0.156 or newer;
  0.154.0 does not list them, so run `codex update`.
- **Daybreak builds are preferred when the account has them.** A job on a model
  with a Daybreak build runs as that build if the account's *live* catalog
  lists it. Today that means `gpt-5.6-sol` becomes `gpt-daybreak-blue-latest`,
  which OpenAI documents as that same snapshot under the Daybreak Blue cyber
  program. `gpt-6-sol` is unaffected: Daybreak is an access program the desktop
  app applies per turn over app-server, and `codex exec` cannot reach it for
  any other model. The static fallback never triggers the swap, because it
  lists Daybreak for every account. A follow-up keeps the model its thread ran
  on unless it names one. `CODEX_PREFER_DAYBREAK=false` turns it off.

### Changed

- **Default is now `gpt-6-sol` at `high`** (was `gpt-5.6-terra` at `xhigh`).
- `/codex:delegate` aliases `sol` and `luna` now mean the GPT-6 models;
  the 5.6 ones remain reachable by full slug. The delegation skill routes to
  GPT-6.
- Quota-exhaustion advice points at `gpt-6-luna`.

### Removed

- **`gpt-5.3-codex-spark`** is retired for ChatGPT accounts: 0.156.1 no longer
  lists it and a live run returns HTTP 400. It is back on the deny-list, with
  `gpt-6-luna` as the suggested replacement, and the `spark` alias is gone.

## [2.2.0] — 2026-09-10

Compatibility pass against **codex-cli 0.154.0**, and support for the models
that shipped with it.

### Added

- **GPT-6 Astra (`gpt-6-astra`)** — OpenAI's most capable model, for complex,
  demanding work. Supports every effort up to `ultra`.
- **Daybreak Blue (`gpt-daybreak-blue-latest`)** — the frontier agentic coding
  model for broad defensive cybersecurity work. It is access-gated, so it
  appears in the catalog only on accounts approved for it, and the delegation
  skill now routes security reviews and hardening work to it when present.
- **`gpt-5.3-codex-spark`** is selectable again — see Fixed. It is the
  ultra-fast tier, for small, mechanical, fully specified edits.
- **Each model's own description is carried through to `codex_models`.** Routing
  advice now comes from the live catalog rather than from a table in this repo
  that goes stale whenever OpenAI ships something.
- Model aliases for `/codex:delegate`: `astra`, `daybreak`, and `spark` join
  `sol`, `terra`, and `luna`.

### Fixed

- **Out-of-quota runs were reported as unknown failures.** codex-cli 0.154.0
  states it in prose — "You've hit your usage limit … or try again at
  `<date>`" — with no `usage_limit_reached` code and no HTTP 429. The
  classifier matched only the older machine-readable spellings, so a plain
  out-of-quota job raised a generic `codex_error` and dropped the advice that
  is the entire point of a typed rate-limit error: switch to a cheaper model,
  lower the effort, or wait for the reset. The reset time is now read from
  either wording, and quoted back with codex's own capitalization.
- **A live model could be blocked by a stale constant.** `validate` consulted
  the built-in deny-list *before* the catalog, so `gpt-5.3-codex-spark` was
  unreachable through this server — rejected as "not available on this
  account" while `codex debug models` listed it as available. The live catalog
  now outranks both `DEPRECATED_MODELS` and `ALIAS_HINTS`; those tables are
  consulted only for slugs the catalog does not know. This is the same rot the
  live catalog was introduced to solve, arriving from the other direction.
- **A standalone `error` event is now read.** Codex usually emits one alongside
  `turn.failed`, but not always. When it was the only structured account of a
  failure, classification fell through to stderr — a stream shared with every
  MCP server in the user's codex config, where a bystander's routine `HTTP 401`
  reads exactly like an expired session. A real `turn.failed` still outranks it.

### Changed

- The fallback catalog is refreshed to the 0.154.0 generation. `gpt-5.4` and
  `gpt-5.4-mini` have been retired from the ChatGPT-account catalog and now
  fail with a pointer to a live model instead of a bare HTTP 400.
- 323 tests, up from 292.

### Verified against codex-cli 0.154.0

Re-checked rather than assumed, since these are the assumptions the server is
built on. Unchanged: `codex exec` still accepts `--sandbox`, `--color`,
`-C/--cd`, `--skip-git-repo-check`, `--json`, `-o`, and `--output-schema`;
`codex exec resume` still rejects `--sandbox`, `--color`, and `-C/--cd`, so
sandbox on a resume still goes through `-c sandbox_mode=`; `-c` values are
still parsed as TOML; and the `--json` event stream still emits
`thread.started`, `turn.completed`, `turn.failed`, and `item.completed` with
the item types the progress phases key off. A live end-to-end `codex exec` run
confirmed the stream shape rather than the help text alone.

## [2.1.1] — 2026-08-10

### Fixed

- **The MCP now finds one stable Codex CLI across nvm projects.** The launcher
  prioritizes `~/.local/bin` before starting the server, so a user-maintained
  Codex launcher remains discoverable when Claude starts the plugin outside an
  interactive login shell or the target project selects a different Node
  version. The normal PATH lookup remains unchanged when that directory is not
  present.
- **Server-tool tests no longer inherit a developer's live model selection.**
  The tests now isolate `.env` and `CODEX_*` model settings, so their documented
  default assertions are repeatable even when an installation intentionally
  configures another default model.

### Added

- A launcher regression test covering stable user-command-directory precedence.

## [2.1.0] — 2026-07-15

A version bump so `error_type` reliably reaches callers.

The field was added late in 2.0.0's development, while reviewing PR #1, and never
recorded here. By then a pre-merge 2.0.0 had already been installed and cached —
and because the version string never moved, that cache had no reason to refresh.
So two materially different trees both answer to "2.0.0": one that returns
`error_type`, one that does not. A caller that branches on the field gets `None`
from the older tree and quietly treats every rate limit as an outage, which is
exactly the failure the field exists to prevent.

Nothing in the server changes here. This release exists to give stale installs a
version to move to, and to finally write the field down.

### Added

- **`error_type` on every read path — now documented.** `codex_status` and
  `codex_result` return a failed job's classification alongside the `error` prose:
  `rate_limit`, `auth_error`, `codex_not_found`, `codex_error`, or `worker_error`;
  `null` on a healthy job. Branch on it rather than pattern-matching the message,
  which is written for the user and will change. The values are stable slugs, not
  exception class names, so an internal rename cannot quietly break a caller. Full
  table in [skills/codex-delegation](skills/codex-delegation/SKILL.md).

### Upgrading

If you installed 2.0.0 before PR #1 merged, you are running a tree that predates
that review's fixes — `error_type`, the three-state `verified` verdict, the
widened deny-list, and the enforced codex timeout among them. Reinstall to pick
them up; the version change is what lets the install see there is anything to do.

## [2.0.0] — 2026-07-15

Turns a review-only server into a general delegation server: Claude Code can now
hand Codex any task and manage it like a subagent. Adds the GPT-5.6 model class,
background jobs, session resume, and verification grounded in git.

**Upgrading from 1.x** — see [Upgrading from
1.x](README.md#upgrading-from-1x) in the README. The short version:

1. `claude mcp remove codex-review-server`, then install the plugin. Leaving the
   old registration in place gives you two copies of every tool.
2. Tools now return a `job_id` instead of blocking for output. Collect with
   `codex_result(job_id)`, or keep the old blocking feel with
   `codex_status(job_id, wait=True)`.
3. If your `.env` pins `CODEX_REVIEW_MODEL=gpt-5.3-codex`, change it — that
   model is deprecated and returns HTTP 400 on every call. Deleting the line
   takes the new default.
4. `CODEX_REVIEW_REASONING=none` is honoured as `low` and warned about at
   startup. No model in the GPT-5.6 catalog offers a `none` level, so the value
   1.x documented no longer exists; nothing breaks, but update the line.

### Fixed

- **The default model was dead.** `gpt-5.3-codex` was deprecated server-side by
  OpenAI and returned HTTP 400 (`"not supported when using Codex with a ChatGPT
  account"`) on every call. Any install running the shipped default was broken.
  Default is now `gpt-5.6-terra`, and known-dead slugs are rejected up front with
  a pointer to a live model.
- **Truncated filenames in change reports.** `git status --porcelain` encodes
  status in the first two columns, so an unstaged edit begins with a space
  (`" M calc.py"`). Stripping the raw output ate that column and shifted every
  path by one character — `calc.py` was reported as `alc.py`. Git output is no
  longer stripped before parsing.
- **`verify_command` ran against the wrong Python.** Child processes inherited
  this server's virtualenv, so `pytest` resolved against the *server's*
  environment instead of the target project's — reporting "No module named
  pytest" for projects that have it. The server's venv is now stripped from
  child environments.
- **Crashed jobs could hang at `running` forever.** Workers were children of the
  MCP server, and an exited-but-unreaped child is a zombie — which still answers
  `kill(pid, 0)`. Liveness checks believed dead workers were alive. Workers now
  detach and reparent to init, so a dead worker's job settles on the next read.
- Rate-limit errors now distinguish quota exhaustion (`usage_limit_reached`,
  `insufficient_quota`) from transient throttling, and surface the reset time
  when Codex reports one.

The following were found by having Codex (`gpt-5.6-sol`, `xhigh`) review this
branch through the server itself, then reproducing each one:

- **Verification went blind on a dirty working tree.** Delegating with
  uncommitted work present is normal, but a file that was already modified stays
  `" M"` when edited again, and a same-line-count edit leaves `--numstat`
  identical too; untracked files never appear in numstat at all. Changes were
  missed *and* falsely reported as "Codex may be claiming work it did not do".
  Baseline-dirty and untracked files are now content-hashed.
- **The model catalog cache ignored `CODEX_HOME`.** Switching ChatGPT accounts
  mid-session — a documented workflow — served the previous account's catalog
  for up to five minutes. The cache is now keyed by home directory.
- **A failed `git status` was reported as "nothing changed"**, so a job whose
  verification never ran could still report `verified: true`. It now fails
  explicitly rather than turning "could not inspect" into "verified".
- **Prompt and stderr sidecar files were never pruned**, ignoring
  `CODEX_MAX_JOBS` and leaving prompt context on disk indefinitely.
- **A cancel arriving before the worker published its pid was lost**, and the
  job ran to completion reporting `completed`. The worker now checks the cancel
  sentinel before starting and again after publishing its pid.
- **A failed worker launch left a job queued forever** — reconcile skips pid-less
  queued records and prune spares active ones. Unclaimed jobs now fail after a
  grace period.
- **PID reuse could kill an unrelated process group.** Worker identity is now
  confirmed before a pid is trusted or signalled.
- **Job ids were used to build paths directly**, so a reference containing `..`
  could point outside the jobs directory. Ids are now shape-validated and prefix
  lookups resolve only against enumerated records.
- **Codex could be orphaned if the event loop raised**, continuing to edit files
  and burn quota unwatched. Cleanup is now unconditional.
- **Timeouts reported `failed` or `timeout` depending on whether partial output
  happened to exist.** Identical events now report identically.

### Added

- **GPT-5.6 model class**: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, with
  the new `max` and `ultra` reasoning efforts.
- **Per-model effort validation.** Effort support is not uniform — Luna has no
  `ultra`; the 5.4/5.5 family has neither `max` nor `ultra`. Invalid pairs are
  rejected before a run starts rather than failing partway through.
- **Live model catalog**, read from `codex debug models` and cached for five
  minutes, with a verified static fallback. New models work without a code
  change; this is the direct fix for the model-rot class of bug above.
- **`codex_delegate`** — delegate any task. Read-only by default; `write=True`
  opts into edits.
- **`codex_follow_up`** — resume a job in its original thread, preserving
  everything Codex read and concluded.
- **`codex_status` / `codex_result` / `codex_cancel`** — job lifecycle, with
  live phase tracking (`investigating`, `editing`, `verifying`, …), an activity
  log, and token usage. `codex_status(wait=True)` blocks until terminal.
- **`codex_models`** — the live catalog for your account.
- **Verification.** Every job is checked against the repository rather than the
  model's self-report: which files git shows changed, whether a read-only job
  respected its sandbox, and whether your `verify_command` actually passed.
- **Structured output.** Pass `result_schema` and Codex's final message is a
  validated JSON object, returned as `structured_output`.
- **Claude Code plugin packaging** — MCP server declared inline in
  `plugin.json` rather than a root `.mcp.json`. A `.mcp.json` at a repo root is
  Claude Code's *project-scope* convention, and this plugin's root is a repo
  people open — so Claude Code loaded it as a project server, where
  `${CLAUDE_PLUGIN_ROOT}` does not expand, and the server died with `ENOENT`
  for anyone who opened the repo. Verified by installing the plugin for real:
  the server registers, connects, and answers tool calls.

  Also ships `.claude-plugin/`, seven slash commands
  (`/codex:delegate`, `/codex:review`, `/codex:status`, `/codex:result`,
  `/codex:follow-up`, `/codex:cancel`, `/codex:models`), and a `codex-delegation`
  skill covering model choice, verification discipline, and fan-out. The
  launcher bootstraps its own virtualenv on first run.
- **Test suite** — 292 tests, no Codex calls required.

### Added — concurrency safety and process teardown

- **Reader/writer lock per working tree.** A write job now takes the tree
  exclusively; readers may share with other readers. Conflicting launches are
  rejected with `repo_busy` naming the blocking job, instead of two jobs quietly
  corrupting each other's edits or a reader reporting a writer's changes as its
  own read-only violation. Enforced atomically, since parallel tool calls
  genuinely race in FastMCP's thread pool.

  Worktree isolation was evaluated and rejected: a worktree is built from a
  commit, so it contains neither uncommitted work (Codex would silently edit
  stale code and report success) nor untracked files like `.env`/`.venv` (so
  `verify_command` would fail). Refusing the unsafe combination is the honest
  trade; the reference implementations only document the hazard.

- **Worker identity is now provable.** A recorded pid is trusted only if the
  process is running `worker.py`, carries *this* job's id in its argv, and
  matches the start-time token captured when the worker published its pid. Both
  the start time and command line come from a single `ps` call, so this costs
  nothing over the previous weaker check. Previously a recycled pid could make a
  dead job look alive forever, or make cancellation signal an unrelated process
  group.

- **Orphaned codex processes are reaped.** The codex pid is recorded, workers
  turn SIGTERM/SIGINT into a clean shutdown that stops codex on the way out, and
  reconciliation kills any codex a dead worker left behind. The server also
  sweeps unfinished jobs from a previous session at startup. An orphaned codex
  keeps editing files, holds its own MCP servers open, and burns quota
  indefinitely — one was found on the development machine still running after
  seven days.

### Changed

- **BREAKING: every tool returns a `job_id` instead of blocking for output.**
  `codex_review`, `codex_review_and_fix`, and `codex_fix` now start background
  jobs. Collect results with `codex_result(job_id)`, or block with
  `codex_status(job_id, wait=True)`. This is what makes parallel fan-out possible
  and stops a 20-minute run from occupying the session.
- **BREAKING: `CODEX_REVIEW_*` env vars renamed to `CODEX_*`** — `CODEX_MODEL`,
  `CODEX_EFFORT`, `CODEX_TIMEOUT`, `CODEX_FOCUS`, `CODEX_HOME_DIR`. The old names
  are still honoured as a fallback, so existing `.env` files keep working.
- Default timeout raised to 4500s, and `.env.example` no longer disagrees with
  the code about it.
- Prompts rebuilt around explicit follow-through, grounding, and self-verification
  contracts. Write tasks are now told not to create git commits, so changes stay
  in the working tree for review.
- Codex is invoked with `--json`, streaming its event log for progress, thread id,
  and token accounting.
- The prompt is passed over stdin rather than argv — task text routinely contains
  backticks and `$()`, and no shell is invoked for codex at all.
- `codex_fix` now requires non-empty findings.
- Auth failures raise a distinct `CodexAuthError` rather than a generic error.

### Notes

- Stays on `codex exec` rather than moving to `codex app-server`. The app-server
  protocol is marked experimental and versioned, and OpenAI's own plugin carries
  compatibility shims for its drift. `exec` is stable and — contrary to its
  published config reference — supports `--output-schema`, so structured output
  costs nothing in stability.
- Verified against codex-cli 0.144.4 on 2026-07-15. `codex exec resume` does not
  accept `--sandbox`, `--color`, or `-C`; sandbox on a resume is set via
  `-c sandbox_mode=`.

## [1.0.0] — 2026-03-26

Initial release: repo-aware cross-model code review.

- `codex_review_and_fix`, `codex_review`, `codex_fix`
- Codex runs as a full agent in the target repository, not against a diff blob
- ChatGPT subscription auth via Codex CLI; the server never handles credentials
- Live-reloaded configuration from `.env`
- P0-P3 priority scheme with auto-fix limited to clear-cut findings
