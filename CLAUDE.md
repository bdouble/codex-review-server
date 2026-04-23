# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An MCP server that gives Claude Code access to OpenAI's Codex CLI for cross-model code review. Codex runs as a full agent inside the target repository (not against a diff blob), using ChatGPT subscription auth — no API keys.

Three tools: `codex_review_and_fix` (review + auto-fix clear P0-P2), `codex_review` (read-only findings), `codex_fix` (apply approved findings).

## Project Structure

This is a small, three-file Python project:

- **`server.py`** — FastMCP server definition. Registers the three MCP tools, handles error responses, runs via stdio transport.
- **`codex_runner.py`** — Subprocess management. Builds prompts, spawns `codex exec` in the target repo's directory, captures output via `-o` flag. Three public functions: `run_review_and_fix`, `run_review_only`, `run_fix`.
- **`config.py`** — Live-reloaded configuration via `classproperty` descriptors. Re-reads `.env` on every property access so changes take effect without restart.

## Development Commands

```bash
# Activate venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the server directly (stdio transport)
python3 server.py

# Test server imports
python3 -c "from server import server; print(f'Server: {server.name} — OK')"

# Test Codex CLI independently
codex exec "Reply with just hello" --sandbox read-only --skip-git-repo-check --color never

# Register with Claude Code (global)
claude mcp add --scope user codex-review-server -- \
  $(pwd)/.venv/bin/python3 $(pwd)/server.py
```

No test suite exists. The only dependency is `mcp>=1.0.0` (which pulls in `python-dotenv` transitively).

## Architecture Notes

- **Config is live-reloaded**: `Config` uses `classproperty` descriptors that call `_reload_env()` (re-reads `.env`) on every access. This means env var changes take effect on the next tool call without restarting the MCP server.
- **Codex runs in the target repo**: `_run_codex()` sets `cwd=project_dir`, giving Codex full file access. Sandbox mode is `read-only` for reviews, `workspace-write` for fixes.
- **Output capture**: Uses `codex exec -o <output_file>` to capture Codex's final message to `/tmp/codex-review-output.txt` (or `-fix-output.txt`). Falls back to stdout if no output file.
- **Auth is delegated**: The server never touches credentials. It sets `CODEX_HOME` in the subprocess env so Codex CLI finds its `config.toml`.
- **Error handling**: Three custom exceptions (`CodexError`, `CodexRateLimitError`, `CodexNotFoundError`) with pattern matching on stderr for rate limits (429), auth failures (401/login), and general errors.

## Environment Variables

All optional; configured in `.env` (see `.env.example`):

| Variable | Default | Notes |
|----------|---------|-------|
| `CODEX_REVIEW_MODEL` | `gpt-5.3-codex` | Must start with `gpt-` or `o` |
| `CODEX_REVIEW_REASONING` | `xhigh` | `none`/`low`/`medium`/`high`/`xhigh` |
| `CODEX_REVIEW_TIMEOUT` | `1500` | Seconds; repo-aware reviews can take 10-20 min |
| `CODEX_REVIEW_FOCUS` | `all` | `bugs`/`security`/`performance`/`all` |
| `CODEX_REVIEW_HOME` | `~/.codex` | Path to Codex CLI credentials directory |
