# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An MCP server + Claude Code plugin that delegates **any** task to OpenAI's Codex
CLI — engineering, research, writing — so Claude Code can orchestrate Codex like
a subagent. Codex runs as a full agent inside the target repository (not against
a diff blob), using ChatGPT subscription auth — no API keys.

Cross-model code review is still here; it's now one job type among many.

Every task is a background job: tools return a `job_id` immediately, and the
caller polls. Nine tools: `codex_delegate`, `codex_follow_up`, `codex_status`,
`codex_result`, `codex_cancel`, `codex_models`, `codex_review`,
`codex_review_and_fix`, `codex_fix`.

## Project Structure

- **`server.py`** — FastMCP server. Registers the nine tools, validates requests,
  spawns workers, formats JSON responses. Runs via stdio transport.
- **`codex_runner.py`** — Builds codex argv and prompts; runs codex while
  streaming its `--json` event log. Typed errors (`CodexError`,
  `CodexRateLimitError`, `CodexNotFoundError`, `CodexAuthError`).
- **`models.py`** — Live model catalog from `codex debug models` (5-min cache,
  static fallback) plus per-model effort validation.
- **`jobs.py`** — On-disk job store: atomic writes, prefix resolution, pruning,
  and reconciliation of jobs whose worker died.
- **`worker.py`** — Detached per-job worker. Daemonizes, runs codex, verifies,
  records the terminal state.
- **`verify.py`** — Git-grounded verification and the `verify_command` runner.
- **`config.py`** — Live-reloaded config via `classproperty`, plus
  `subprocess_env()`.
- **`.claude-plugin/`, `commands/`, `skills/`** — plugin packaging. The MCP
  server is declared inline in `plugin.json`; a root `.mcp.json` would be
  loaded as *project* config, where `${CLAUDE_PLUGIN_ROOT}` does not expand.
- **`tests/`** — 292 pytest tests. No Codex calls; the CLI is stubbed.

## Development Commands

```bash
# Set up
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# Test (fast, offline)
.venv/bin/python3 -m pytest tests/ -q

# Server smoke test
.venv/bin/python3 -c "from server import server; print(server.name, '— OK')"

# Full MCP handshake over stdio
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | ./scripts/run-server.sh

# Check the live model catalog
codex debug models | python3 -m json.tool | head -40

# Register with Claude Code (global)
claude mcp add --scope user codex-delegate -- $(pwd)/.venv/bin/python3 $(pwd)/server.py
```

## Architecture Notes

- **Everything is a job.** Tools validate, write a job record, spawn a detached
  worker, and return a `job_id`. Reads go through the job store. Long
  `max`/`ultra` runs therefore never block the MCP client, and fan-out is free.
- **Workers detach to init** (`worker._daemonize`). This is correctness, not
  tidiness: an unreaped zombie child still answers `kill(pid, 0)`, so a crashed
  worker would pin its job at `running` forever. Reparenting makes PID liveness
  truthful for `jobs.reconcile()`, which settles dead-worker jobs at read time.
- **Codex leads its own process group**, and everything that stops it signals
  that group rather than the bare pid. This is what makes `CODEX_TIMEOUT`
  mean anything: codex spawns MCP servers and shells, they inherit its stdout
  pipe, and killing codex alone leaves them holding the write end open — the
  reader never sees EOF, so the watchdog fires and `run_codex` blocks on
  anyway, job pinned at `running` with its deadline long past. Reaping the
  group closes the pipe. `codex_cancel` reaches codex through
  `reap_orphan_codex` (which targets codex's own pgid), and the worker's
  SIGTERM handler unwinds into `run_codex`'s cleanup, so both paths still
  cover it.
- **`codex exec`, not `codex app-server`.** The app-server protocol is
  experimental and versioned. `exec` is stable and supports `--output-schema`
  despite the published config reference claiming otherwise.
- **Prompts go over stdin (`-`), never argv.** Task text routinely contains
  backticks and `$()`. No shell is invoked for codex.
- **Config is live-reloaded**: `Config` re-reads `.env` on every property access,
  so changes apply on the next tool call without a restart.
- **`subprocess_env()` strips this server's venv** from children, so
  `verify_command` sees the target project's environment, not ours.
- **Verification is git-grounded.** `verify.py` snapshots before, compares after,
  and never trusts Codex's account of what it changed.

## Codex CLI Gotchas

Verified against codex-cli 0.144.4 on 2026-07-15. These contradict parts of the
published docs — trust the CLI, and re-verify with `codex exec --help` before
assuming.

- **`codex exec resume` has a different flag set than `codex exec`.** It rejects
  `--sandbox`, `--color`, and `-C/--cd`. Sandbox on a resume must go through
  `-c sandbox_mode="..."`.
- **`-c` values are parsed as TOML**, so strings need quotes:
  `-c model_reasoning_effort='"xhigh"'`.
- **Effort support is per-model.** Luna has no `ultra`; 5.4/5.5 have neither
  `max` nor `ultra`. `codex debug models` is the authority.
- **Bare `gpt-5.6` fails under ChatGPT auth** — full slugs only.
- **`gpt-5.3-codex` / `gpt-5.2` are deprecated** and return HTTP 400.
- **stderr carries noise on successful runs** (other MCP servers in the user's
  codex config log auth errors there). Only classify errors when the exit code
  is non-zero.
- `--json` emits `thread.started` (the id for resume) and `turn.completed`
  (token usage).

## Environment Variables

All optional; configured in `.env` (see `.env.example`). The older
`CODEX_REVIEW_*` names still work as a fallback.

| Variable | Default | Notes |
|----------|---------|-------|
| `CODEX_MODEL` | `gpt-5.6-terra` | Validated against the live catalog |
| `CODEX_EFFORT` | `xhigh` | `low`/`medium`/`high`/`xhigh`/`max`/`ultra` |
| `CODEX_TIMEOUT` | `4500` | Seconds; repo-aware work takes 10-20 min, `ultra` longer |
| `CODEX_FOCUS` | `all` | `bugs`/`security`/`performance`/`all` |
| `CODEX_HOME_DIR` | `~/.codex` | Codex CLI credentials directory |
| `CODEX_STATE_DIR` | `~/.codex-review-server` | Job records |
| `CODEX_MAX_JOBS` | `50` | Retained job records before pruning |
