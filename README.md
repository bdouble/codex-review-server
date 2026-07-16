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
| `git_tracking` | not a git repo | `skip` — nothing could be inspected, so `verified` comes back `null` rather than `true` |

`verified` has three states, because "clean" and "unchecked" are different
claims and a boolean cannot tell them apart:

| `verified` | Meaning |
|---|---|
| `true` | Every applicable check ran, and none failed |
| `false` | A check ran and actively failed — something is wrong |
| `null` | A check could not run. Nothing is known either way — the usual case for a task in an unversioned directory |

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
claude plugin install codex-delegate@codex-delegate
```

The repo is its own single-plugin marketplace, so the name repeats — the first
`codex-delegate` is the plugin, the second is the marketplace.

This registers the MCP server, the seven slash commands, and the
`codex-delegation` skill. The launcher creates its own virtualenv and installs
dependencies on first run — no manual setup.

Verify with `claude mcp list`, which should show:

```
plugin:codex-delegate:codex-delegate: .../scripts/run-server.sh - ✔ Connected
```

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

### Install gotchas

- **You must start a new session.** MCP servers load at session start, so the
  tools will not appear in the session where you installed them.
- **The first run is slow.** The plugin launcher creates a virtualenv and pip
  installs into it, which takes a few seconds. It logs progress to stderr; if
  it were to write to stdout it would corrupt the MCP protocol stream.
- **Python 3.10+ is required**, and `python3` must be on `PATH`. To pin a
  specific interpreter, set `CODEX_MCP_PYTHON` — but then dependencies must
  already be installed there (`$CODEX_MCP_PYTHON -m pip install -r
  requirements.txt`); the launcher will not bootstrap an interpreter you chose
  explicitly.
- **Don't add a `.mcp.json` to this repo.** A `.mcp.json` at a repo root is
  Claude Code's *project-scope* convention, and this plugin's root is a repo you
  may open. Claude Code would load it as a project server — a context where
  `${CLAUDE_PLUGIN_ROOT}` does not expand — and every session in the repo would
  fail with `ENOENT`. The server is declared inline in `plugin.json` for exactly
  this reason, and a test enforces it.
- **`verify_command` runs against your project's Python, not this server's.**
  The server's virtualenv is stripped from child processes, so `pytest` resolves
  in the target project. If your project needs its venv active, say so in the
  command: `--verify ".venv/bin/pytest -q"`.

## Upgrading from 1.x

1.x was review-only and blocking. Two things will break if you skip this.

### 1. Remove the old registration

The 1.x install registered a server named `codex-review-server`. Leave it in
place and you'll have **two copies of every tool** with different names, both
running the same code:

```bash
claude mcp remove codex-review-server
```

Then install the plugin (above), and start a new session. Confirm with
`claude mcp list` — you want `plugin:codex-delegate:codex-delegate` and no
`codex-review-server`.

### 2. Tools return a job id, not output

This is the breaking change. Every tool now starts a background job:

```python
# 1.x — blocked until Codex finished, returned the findings
codex_review(project_dir="/repo")
  -> {"status": "complete", "output": "## Findings\nP1: ..."}

# 2.0 — returns immediately
codex_review(project_dir="/repo")
  -> {"status": "started", "job_id": "review-abc123-9f2e1c"}
codex_status("review-abc123-9f2e1c", wait=True)   # blocks until done
codex_result("review-abc123-9f2e1c")              # findings + verification
```

If you want the old blocking feel, `codex_status(job_id, wait=True)` is the
single call that gives it. The upside is that a 20-minute run no longer occupies
your session, and you can run several jobs at once.

### 3. Environment variables were renamed (non-breaking)

`CODEX_REVIEW_*` → `CODEX_*`. The old names are still honoured as a fallback, so
an existing `.env` keeps working — but new installs should use the names in
[Configuration](#configuration). One exception worth acting on: if your `.env`
pins `CODEX_REVIEW_MODEL=gpt-5.3-codex`, that model is **dead** and every call
will fail with HTTP 400. Change it, or delete the line to take the new default.

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

In practice you just ask, and Claude drives the tools:

```
Delegate to Codex: find every caller of authenticate() that ignores its return
value, and report which are exploitable.
```

Let it edit, and prove it worked:

```
/codex:delegate --write --verify "pytest -q" Fix the race in the session cache
```

Cross-model review, then continue in the same thread:

```
/codex:review main --fix
/codex:follow-up review-abc123 Now add regression tests for the case you found
```

### The job lifecycle

Every tool starts a background job and hands back an id. The full loop:

```python
codex_delegate(task="...", project_dir="/repo", write=True,
               verify_command="pytest -q")
  -> {"job_id": "delegate-abc123-9f2e1c", "sandbox": "workspace-write"}

codex_status("delegate-abc123")          # prefix is enough
  -> {"status": "running", "phase": "editing", "elapsed_seconds": 41.2}

codex_status("delegate-abc123", wait=True)   # or just block
  -> {"status": "completed", "phase": "done"}

codex_result("delegate-abc123")
  -> {"output": "## Summary\n...",
      "verification": {"verified": true, ...},
      "usage": {"output_tokens": 4200}}
```

`phase` tracks what Codex is doing right now — `investigating`, `editing`,
`verifying` — so a long run is legible rather than a black box. `codex_cancel`
stops a job and its Codex process. Omit the id from `codex_status` to list
every job.

### Running jobs in parallel

Jobs are independent, so fan-out is just launching several:

```
Have Codex audit each of these three repos for injection bugs, one job each.
```

**Across different repos.** Within one working tree the server enforces one
writer at a time (readers may share) — a second conflicting job is rejected with
`repo_busy` naming the blocker. That's not a limitation so much as an admission:
Codex edits a real working tree, and two writers in one tree corrupt each other.

### Structured output

Pass `result_schema` and Codex's final message is a JSON object validated
against it, returned as `structured_output` — better than parsing prose:

```python
codex_delegate(
    task="Analyze each public function's error handling.",
    project_dir="/repo",
    result_schema={
        "type": "object",
        "properties": {
            "functions": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "has_error_handling": {"type": "boolean"}}}},
            "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["functions", "risk_level"],
    },
)
# -> structured_output: {"functions": [...], "risk_level": "low"}
```

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
.venv/bin/python3 -m pytest tests/ -q      # 233 tests, no Codex calls needed
```

The suite stubs the CLI, so it's fast and offline. It covers command
construction (including the exec/resume flag divergence), git porcelain parsing,
job-store reconciliation, worker identity, the repo lock, error classification,
plugin manifests, and every tool's validation path.

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

**`repo_busy`** — another job holds that working tree. Wait
(`codex_status(id, wait=True)`), cancel the blocker, or use a different repo.
Not a bug: two jobs writing one tree corrupt each other.

**Server not appearing** — `claude mcp list`, then start a *new* session.

**`Failed to reconnect: ENOENT` + `Missing environment variables:
CLAUDE_PLUGIN_ROOT`** — something is loading this plugin's server as a
*project* server, where that variable doesn't expand. Check for a `.mcp.json` in
the repo root (there shouldn't be one — see [Install
gotchas](#install-gotchas)), and for a stale `enabledMcpjsonServers` entry in
`.claude/settings.local.json` pointing at a server that no longer exists.

**Two of every tool** — you have both the 1.x `codex-review-server` and the
plugin registered. `claude mcp remove codex-review-server`; see [Upgrading from
1.x](#upgrading-from-1x).

**`No module named pytest` from `verify_command`** — the command runs in your
project, not this server's virtualenv. Point it at the right interpreter:
`--verify ".venv/bin/pytest -q"`.

Test the pieces independently:

```bash
codex exec "Reply with just hello" --sandbox read-only --skip-git-repo-check
.venv/bin/python3 -c "from server import server; print(server.name, '— OK')"
```

## Uninstall

```bash
claude plugin uninstall codex-delegate     # plugin install
claude mcp remove codex-delegate           # manual install
claude mcp remove codex-review-server      # 1.x install, if still registered
rm -rf ~/.codex-review-server              # job records
```

Removing the server doesn't cancel jobs already running — workers are detached
so they survive a server restart. Check with `codex_status` and `codex_cancel`
anything in flight *before* uninstalling, or those Codex processes will run to
completion on their own.

## License

MIT
