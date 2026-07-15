# Codex Delegate

An MCP server that lets Claude Code delegate **any** task to OpenAI's Codex CLI —
engineering, research, analysis, writing — and manage it the way it manages its
own subagents.

Codex runs as a full agent inside your repository: reading files, exploring
dependencies, running commands. Not a diff blob in a sandbox — the same thing
you'd get running `codex` yourself.

Authentication is your ChatGPT subscription (`codex login`). No API keys, no
per-token billing. This server never sees your credentials.

> Formerly *Codex Review MCP Server*. It still does cross-model review — that's
> now one job type among many. See [CHANGELOG.md](CHANGELOG.md) for the 2.0
> breaking changes.

## Why this exists

Claude Code is good at orchestrating. Codex is a genuinely different model with
different training and different blind spots. Delegating to it gets you a second
opinion that isn't just your own reasoning restated — most valuable exactly where
you're most likely to be wrong.

Three things here are deliberate, and are what distinguish this from just
shelling out to `codex exec`:

1. **Every task is a background job.** Launch returns a job id immediately.
   Poll it, cancel it, run ten in parallel. A 20-minute `ultra` run never blocks
   your session.
2. **Results are verified against the repo, not the model's word.** Codex saying
   "done" is a claim. This server checks it against git and your test command.
3. **Follow-ups resume the thread.** Codex keeps everything it read and
   concluded, instead of re-deriving it from scratch.

## What it does

| Tool | What it's for |
|------|---------------|
| `codex_delegate` | Delegate any task. Returns a job id. Read-only unless `write=True`. |
| `codex_follow_up` | Continue a job in its original thread, with all its context intact. |
| `codex_status` | Progress and phase. Omit the id to list every job. `wait=True` blocks. |
| `codex_result` | Full output plus the verification report. |
| `codex_cancel` | Stop a job and its Codex process — no orphans burning quota. |
| `codex_models` | Live model catalog for your account, with valid efforts. |
| `codex_review` | Cross-model review of a branch. Read-only, prioritized findings. |
| `codex_review_and_fix` | Review, then auto-fix only the clear-cut P0-P2 findings. |
| `codex_fix` | Apply specific findings you approved. |

Slash commands: `/codex:delegate`, `/codex:review`, `/codex:status`,
`/codex:result`, `/codex:follow-up`, `/codex:cancel`, `/codex:models`.

## Models

The GPT-5.6 class, as three durable capability tiers:

| Model | Best for | Efforts |
|-------|----------|---------|
| `gpt-5.6-sol` | Frontier. Ambiguous, difficult, high-value work. | low → xhigh, `max`, `ultra` |
| `gpt-5.6-terra` | The pragmatic all-rounder. **Default.** | low → xhigh, `max`, `ultra` |
| `gpt-5.6-luna` | Fast. Extraction, classification, structured summaries. | low → xhigh, `max` |
| `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini` | Previous generation. | low → xhigh |

Default: **`gpt-5.6-terra` at `xhigh`**.

Things that will bite you if you don't know them — all enforced by the server:

- **Effort validity is per-model.** Luna has no `ultra`. The 5.4/5.5 family has
  neither `max` nor `ultra`. An invalid pair is rejected up front rather than
  failing ten minutes in.
- **`ultra` coordinates four agents in parallel.** Much slower and costlier.
- **Sol defaults to `low`** and is strong there. Start lower than you'd think.
- **Use the full slug.** Bare `gpt-5.6` does not resolve under ChatGPT auth.
- **`gpt-5.3-codex` and `gpt-5.2` are dead** for ChatGPT accounts. Rejected with
  a pointer to a live model.

The catalog is read live from the Codex CLI (`codex_models`), so a model
released after this server was written works without a code change. That isn't
gold-plating: the previous hardcoded default was retired by OpenAI and broke
every call.

## Verification

The part neither reference implementation does. When a job finishes, the server
compares the repo against a snapshot taken before the run:

```json
"verification": {
  "git": {
    "committed": false,
    "files_changed": ["calc.py", "test_calc.py"],
    "diff_stat": "calc.py | 2 ++"
  },
  "checks": [
    {"name": "files_changed", "status": "pass", "detail": "2 file(s) changed per git."},
    {"name": "verify_command", "status": "pass", "detail": "`pytest -q` exited 0."}
  ],
  "verified": true
}
```

| Check | Fires when | Meaning |
|---|---|---|
| `files_changed` | write job | `warn` if git shows nothing changed — it may be claiming work it didn't do |
| `read_only_respected` | read-only job | `fail` if files changed anyway |
| `verify_command` | you passed one | `fail` on a non-zero exit — the real one, from your own tests |
| `git_tracking` | not a git repo | `skip` — file claims are unverifiable |

Pass `verify_command` whenever the repo has tests. It's the difference between
Codex reporting success and success being observed.

Two limits worth knowing:

- **`verify_command` runs on the host, outside Codex's sandbox.** `write=False`
  constrains Codex, not your test command. It comes from you rather than the
  model, so Codex can't inject one — but don't pass something you wouldn't run
  yourself. It executes *after* file changes are computed, so test caches aren't
  misattributed to Codex.
- **Verification reads global git state**, so it can't tell one actor from
  another. The server enforces one writer per working tree (readers may share);
  a conflicting launch is rejected with `repo_busy` naming the blocker. Parallel
  jobs want separate repos. The lock can't stop *you*, though — your own edits
  during a job get attributed to it.

## Install

### Prerequisites

**Python 3.10+**, **Codex CLI**, and a **ChatGPT subscription** (Plus or Pro):

```bash
python3 --version        # 3.10+
npm i -g @openai/codex   # see github.com/openai/codex for other platforms
codex login              # opens browser; credentials land in ~/.codex
```

### As a Claude Code plugin (recommended)

```bash
claude plugin marketplace add bdouble/codex-review-server
claude plugin install codex-delegate
```

This registers the MCP server, the slash commands, and the `codex-delegation`
skill. The launcher creates its own virtualenv and installs dependencies on
first run — no manual setup.

### As a plain MCP server

```bash
git clone https://github.com/bdouble/codex-review-server.git ~/.claude/mcp/codex-review-server
cd ~/.claude/mcp/codex-review-server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

claude mcp add --scope user codex-delegate -- \
  ~/.claude/mcp/codex-review-server/.venv/bin/python3 \
  ~/.claude/mcp/codex-review-server/server.py
```

Start a **new** session — MCP servers load at session start — and ask
*"what codex tools are available?"* to confirm.

## Configuration

All optional; defaults work. Copy `.env.example` to `.env` and edit. Config is
**live-reloaded** on every tool call, so changes apply to the next call without
a restart.

| Variable | Default | Description |
|----------|---------|-------------|
| `CODEX_MODEL` | `gpt-5.6-terra` | Default model slug |
| `CODEX_EFFORT` | `xhigh` | Default reasoning effort |
| `CODEX_TIMEOUT` | `4500` | Per-job timeout, seconds |
| `CODEX_FOCUS` | `all` | Review focus: `bugs`, `security`, `performance`, `all` |
| `CODEX_HOME_DIR` | `~/.codex` | Codex CLI home (config.toml + credentials) |
| `CODEX_STATE_DIR` | `~/.codex-review-server` | Where job records live |
| `CODEX_MAX_JOBS` | `50` | Job records kept before the oldest are pruned |

The older `CODEX_REVIEW_*` names still work as a fallback, so an existing `.env`
keeps running.

### Multiple ChatGPT accounts

Point `CODEX_HOME_DIR` at another credentials directory, each created with
`CODEX_HOME=/path/to/other/.codex codex login`. Live reload means you can switch
mid-session.

## Usage

Delegate anything:

```
Delegate to Codex: find every caller of authenticate() that ignores its return
value, and report which are exploitable.
```

Fan out in parallel — each call returns a job id immediately:

```
Have Codex audit these three services for injection bugs, one job each.
```

Let it edit, and prove it worked:

```
/codex:delegate --write --verify "pytest -q" Fix the race in the session cache
```

Cross-model review:

```
/codex:review main --fix
```

Continue without losing context:

```
/codex:follow-up task-abc123 Now add regression tests for the case you found
```

Structured data instead of prose — pass `result_schema` and Codex's final
message is a validated JSON object returned as `structured_output`.

## How it works

```
Claude Code
    │ MCP tool call (stdio)
    ▼
server.py ──► jobs/<id>.json          (job record; atomic writes)
    │
    ├─► worker.py (detached, reparented to init)
    │       │
    │       ├─► codex exec --model gpt-5.6-terra -c model_reasoning_effort="xhigh"
    │       │     --sandbox read-only --json -o <out>    (runs IN your repo)
    │       │        │ streams JSONL events → phase, thread_id, token usage
    │       │        ▼
    │       └─► verify.py  ──► git snapshot diff + your verify_command
    │
    ▼
codex_status / codex_result ◄── job record
```

Notes on the implementation, since they're the non-obvious parts:

- **`codex exec`, not `codex app-server`.** The app-server protocol is marked
  experimental and versioned; OpenAI's own plugin carries compatibility shims
  for its drift. `exec` is the stable public interface and — verified, contrary
  to its docs — supports `--output-schema` for structured output.
- **The prompt goes over stdin, never argv.** Task text routinely contains
  backticks and `$()`. No shell is invoked for codex at all.
- **Workers detach to init.** A zombie child still answers `kill(pid, 0)`, which
  would let a crashed worker pin its job at `running` forever. Reparenting makes
  PID liveness truthful, so a dead worker's job settles correctly on the next read.
- **`codex exec resume` has a different flag set** — no `--sandbox`, no
  `--color`, no `-C`. Sandbox on a resume goes through `-c sandbox_mode=`.
- **The server's own venv is stripped** from child environments, so
  `verify_command` sees your project's Python, not this server's.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m pytest tests/ -q      # 148 tests, no Codex calls needed
```

The suite stubs the CLI, so it's fast and offline. It covers command
construction (including the exec/resume flag divergence), porcelain parsing,
job-store reconciliation, error classification, and every tool's validation path.

## Troubleshooting

**"Codex CLI not found"** — `npm i -g @openai/codex`, then `which codex`.

**Authentication failed** — `codex login`. The server can't do this for you.

**Rate limit** — Plus allows 30-150 messages/5hr, Pro 300-1500. The error
distinguishes quota exhaustion (switch to `gpt-5.6-luna`, lower the effort, or
wait for the reset it reports) from transient throttling (just retry).

**`invalid_model`** — a deprecated slug or an effort that model doesn't support.
Run `codex_models` for what's actually available.

**Job times out** — `status: timeout` still returns whatever partial work was
salvaged; treat it as incomplete. Narrow the task, lower the effort, or raise
`CODEX_TIMEOUT`. `ultra` runs four agents and needs real headroom.

**Job stuck `running`** — check `phase` via `codex_status`. `max`/`ultra` runs
are genuinely long. `codex_cancel` if wedged.

**Server not appearing** — `claude mcp list`, then start a *new* session.

Test the pieces independently:

```bash
codex exec "Reply with just hello" --sandbox read-only --skip-git-repo-check
.venv/bin/python3 -c "from server import server; print(server.name, '— OK')"
```

## Uninstall

```bash
claude plugin uninstall codex-delegate     # plugin install
claude mcp remove codex-delegate           # manual install
rm -rf ~/.codex-review-server              # job records
```

## License

MIT
