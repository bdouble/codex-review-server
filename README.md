# Codex Review MCP Server

An MCP server that gives Claude Code access to OpenAI's Codex models for cross-model code review. Uses Codex CLI with ChatGPT subscription authentication — no API key billing required.

## What It Does

- **`codex_review`** — Sends a code diff to GPT-5.3-Codex (or configured model) for adversarial bug detection, security analysis, and edge case identification
- **`codex_fix`** — Generates minimal, surgical fixes for specific findings

## Prerequisites

Before installing the MCP server, you need:

### 1. Python 3.10 or later

Check your version:
```bash
python3 --version
```

If you need to install or upgrade:
```bash
# macOS
brew install python@3.14

# Ubuntu/Debian
sudo apt install python3.12
```

### 2. Codex CLI

Codex CLI is OpenAI's terminal-based coding agent. This MCP server uses it as a bridge to access GPT models.

```bash
# macOS
brew install codex-cli

# Verify installation
codex --version
```

See [github.com/openai/codex](https://github.com/openai/codex) for other platforms.

### 3. ChatGPT Subscription

Codex CLI authenticates through your ChatGPT account. You need an active ChatGPT Plus ($20/month) or Pro ($200/month) subscription. No separate API key or billing is required.

Authenticate Codex CLI (opens your browser):
```bash
codex login
```

This stores credentials in `~/.codex/config.toml`. You only need to do this once — sessions persist until they expire, at which point you run `codex login` again.

### 4. Claude Code

Install from [claude.ai/code](https://claude.ai/code) if you haven't already.

## Installation

### Step 1: Clone the repository

```bash
git clone https://github.com/bdouble/codex-review-server.git ~/.claude/mcp/codex-review-server
```

### Step 2: Create a virtual environment and install dependencies

The MCP SDK requires its own virtual environment:

```bash
cd ~/.claude/mcp/codex-review-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
deactivate
```

### Step 3: Register with Claude Code

Register the server using the venv's Python (so it can find the `mcp` package):

```bash
claude mcp add codex-review-server -- ~/.claude/mcp/codex-review-server/.venv/bin/python3 ~/.claude/mcp/codex-review-server/server.py
```

### Step 4: Verify

Start a new Claude Code session and ask:

```
What MCP tools are available from codex-review-server?
```

Claude should list `codex_review` and `codex_fix`.

For a full smoke test, in any repo with changes on a branch:

```
Use codex_review to review the output of `git diff main...HEAD`
```

## Configuration

All settings are optional. Defaults work out of the box.

Copy the example env file if you want to customize:
```bash
cd ~/.claude/mcp/codex-review-server
cp .env.example .env
# Edit .env as needed
```

Or set variables in your `.claude/settings.json`:
```json
{
  "env": {
    "CODEX_REVIEW_MODEL": "gpt-5.3-codex",
    "CODEX_REVIEW_REASONING": "xhigh"
  }
}
```

### Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `CODEX_REVIEW_HOME` | `~/.codex` | Codex CLI home directory (config.toml + credentials) |
| `CODEX_REVIEW_MODEL` | `gpt-5.3-codex` | OpenAI model ID (must start with `gpt-` or `o`) |
| `CODEX_REVIEW_REASONING` | `xhigh` | Reasoning effort: `none`, `low`, `medium`, `high`, `xhigh` |
| `CODEX_REVIEW_TIMEOUT` | `1500` | Timeout in seconds per call (25 min default — repo-aware reviews are thorough) |
| `CODEX_REVIEW_AUTO_FIX` | `false` | Auto-approve all findings without user interaction |
| `CODEX_REVIEW_MIN_SEVERITY` | `medium` | Minimum severity to report: `critical`, `high`, `medium`, `low` |
| `CODEX_REVIEW_FOCUS` | `all` | Review focus: `bugs`, `security`, `performance`, `all` |

### Reasoning Effort Levels

The `CODEX_REVIEW_REASONING` setting controls how deeply Codex analyzes the code:

| Level | Speed | Cost | Use When |
|-------|-------|------|----------|
| `none` | Fastest | Lowest | Quick syntax checks |
| `low` | Fast | Low | Simple, obvious issues |
| `medium` | Moderate | Moderate | General code review |
| `high` | Slow | High | Complex logic, security |
| `xhigh` | Slowest | Highest | Deep analysis, finding subtle bugs (default) |

Note: "cost" here refers to ChatGPT rate limit consumption, not billing. All usage is covered by your ChatGPT subscription.

## Usage

### Direct tool calls

Once installed, Claude Code can call the tools directly in any conversation:

```
Use codex_review to review this diff for bugs and security issues
```

### With pm-vibecode-ops workflow

If you use the [pm-vibecode-ops](https://github.com/bdouble/pm-vibecode-ops) workflow plugin:

- **`/codex-review [ticket-id]`** — Standalone cross-model review with Linear integration
- **`/execute-ticket [ticket-id]`** — Automatically includes Codex review as Phase 5.5 (between Code Review and Security Review)
- **`/epic-swarm [epic-id]`** — Includes Codex review in each parallel ticket's workflow

### Without this MCP server

The pm-vibecode-ops workflow works fully without this server. `/execute-ticket` skips Phase 5.5 with a note, and `/codex-review` provides installation instructions.

## How It Works

```
Claude Code
    │ (MCP tool call over stdio)
    ▼
server.py (FastMCP)
    │ (subprocess with CODEX_HOME env)
    ▼
codex exec --model gpt-5.3-codex --reasoning-effort xhigh --sandbox read-only
    │ (authenticates via ~/.codex/config.toml — ChatGPT subscription)
    ▼
GPT-5.3-Codex response
    │ (parsed into structured JSON findings)
    ▼
Claude Code receives findings
```

The server never sees or stores your ChatGPT credentials. Authentication is handled entirely by Codex CLI reading its own config directory.

## Troubleshooting

### "Codex CLI not found"

Codex CLI is not installed or not on your PATH.

```bash
# Check if installed
which codex

# Install
brew install codex-cli
```

### "Codex CLI authentication failed"

Your Codex CLI session has expired.

```bash
codex login
```

### "Rate limit reached"

You've hit your ChatGPT subscription's rate limit (30-150 messages per 5-hour window on Plus, 300-1500 on Pro). The server returns a `rate_limit` error and the workflow continues without the Codex review. Wait a few minutes and retry, or run `/codex-review` independently later.

### MCP server not appearing in Claude Code

1. Verify registration:
   ```bash
   claude mcp list
   ```

2. Check the Python path points to the venv:
   ```bash
   # Should show the .venv path, not system Python
   claude mcp list | grep codex-review-server
   ```

3. Re-register if needed:
   ```bash
   claude mcp remove codex-review-server
   claude mcp add codex-review-server -- ~/.claude/mcp/codex-review-server/.venv/bin/python3 ~/.claude/mcp/codex-review-server/server.py
   ```

### "No matching distribution found for mcp"

Your Python version is too old (needs 3.10+), or you're not using the virtual environment.

```bash
# Check Python version inside venv
~/.claude/mcp/codex-review-server/.venv/bin/python3 --version

# If too old, recreate with a newer Python
cd ~/.claude/mcp/codex-review-server
rm -rf .venv
python3.14 -m venv .venv  # or python3.12, python3.13
source .venv/bin/activate
pip install -r requirements.txt
```

### Server starts but tools don't work

Test the components independently:

```bash
# 1. Test Codex CLI directly
codex exec "Reply with just the word hello" --sandbox read-only --skip-git-repo-check --color never

# 2. Test server imports
~/.claude/mcp/codex-review-server/.venv/bin/python3 -c "
import sys; sys.path.insert(0, '$HOME/.claude/mcp/codex-review-server')
from server import server
print(f'Server: {server.name} — OK')
"
```

## Uninstall

```bash
claude mcp remove codex-review-server
rm -rf ~/.claude/mcp/codex-review-server
```
