# Codex Review MCP Server

An MCP server that gives Claude Code access to OpenAI's Codex models for cross-model code review. Codex runs as a full agent in your repository — reading files, exploring dependencies, and understanding context — the same as running it manually in the Codex UI.

Uses Codex CLI with ChatGPT subscription authentication. No API key billing required.

## What It Does

Three tools, matching a natural review workflow:

| Tool | Mode | What Happens |
|------|------|-------------|
| `codex_review_and_fix` | Review + auto-fix | Codex reviews changes, auto-fixes clear P0-P2 issues, reports ambiguous ones with questions, lists P3 for awareness |
| `codex_review` | Review only | Same review, no fixes. Read-only. For when you want findings first. |
| `codex_fix` | Fix only | Applies specific approved findings. Second pass after human review. |

### Priority Scheme

- **P0 (Critical)**: Security vulnerabilities, data loss, crashes, auth bypass
- **P1 (High)**: Significant bugs, logic errors, race conditions, missing error handling
- **P2 (Medium)**: Edge cases, potential issues under specific conditions
- **P3 (Low)**: Minor improvements, suggestions (never auto-fixed)

### Auto-Fix Rules

For P0-P2 findings:
- **Clear-cut** (confident, obvious fix) → auto-fixed
- **Has questions** (ambiguity, trade-offs, needs context) → reported with Codex's question, NOT fixed

P3 findings are always reported only, never fixed.

## Prerequisites

### 1. Python 3.10 or later

```bash
python3 --version  # must be 3.10+

# macOS
brew install python@3.14
```

### 2. Codex CLI

```bash
# macOS
brew install codex-cli

# Verify
codex --version
```

See [github.com/openai/codex](https://github.com/openai/codex) for other platforms.

### 3. ChatGPT Subscription

Codex CLI authenticates through your ChatGPT account (Plus at $20/month or Pro at $200/month). No separate API key needed.

```bash
codex login  # opens browser, sign into ChatGPT
```

Credentials are stored in `~/.codex/config.toml`. Re-run `codex login` if your session expires.

### 4. Claude Code

Install from [claude.ai/code](https://claude.ai/code) if you haven't already.

## Installation

### Step 1: Clone

```bash
git clone https://github.com/bdouble/codex-review-server.git ~/.claude/mcp/codex-review-server
```

### Step 2: Set up virtual environment

```bash
cd ~/.claude/mcp/codex-review-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
deactivate
```

### Step 3: Register with Claude Code

```bash
# Global (available in all projects — recommended)
claude mcp add --scope user codex-review-server -- \
  ~/.claude/mcp/codex-review-server/.venv/bin/python3 \
  ~/.claude/mcp/codex-review-server/server.py

# Or project-scoped (current repo only)
claude mcp add codex-review-server -- \
  ~/.claude/mcp/codex-review-server/.venv/bin/python3 \
  ~/.claude/mcp/codex-review-server/server.py
```

### Step 4: Verify

Start a **new** Claude Code session (MCP servers load at session start) and ask:

```
What MCP tools are available from codex-review-server?
```

Claude should list `codex_review_and_fix`, `codex_review`, and `codex_fix`.

## Configuration

All settings are optional. Defaults work out of the box.

```bash
cd ~/.claude/mcp/codex-review-server
cp .env.example .env
# Edit .env as needed
```

Config is **live-reloaded** on every tool call. Edit `.env` mid-session and the next call uses the new values — no restart needed.

You can also set values in `.claude/settings.json` (env vars override `.env`):

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
| `CODEX_REVIEW_TIMEOUT` | `1500` | Timeout in seconds (25 min default — repo-aware reviews are thorough) |
| `CODEX_REVIEW_FOCUS` | `all` | Review focus: `bugs`, `security`, `performance`, `all` |

### Reasoning Effort Levels

| Level | Use When |
|-------|----------|
| `none` | Quick syntax checks |
| `low` | Simple, obvious issues |
| `medium` | General code review |
| `high` | Complex logic, security |
| `xhigh` | Deep analysis, finding subtle bugs (default) |

"Cost" is ChatGPT rate limit consumption, not billing. All usage is covered by your subscription.

### Multiple Codex Accounts

If you maintain multiple ChatGPT subscriptions, set `CODEX_REVIEW_HOME` to point at a different credentials directory:

```bash
# In .env
CODEX_REVIEW_HOME=/path/to/other/.codex
```

Each directory should have its own `config.toml` from running `CODEX_HOME=/path/to/other/.codex codex login`.

## Usage

### Direct tool calls

In any Claude Code session:

```
Use codex_review_and_fix to review and fix the changes on this branch
```

Claude calls the MCP tool with the project directory and base branch. Codex runs as a full agent — reads files, runs git commands, explores the codebase.

### With pm-vibecode-ops workflow

If you use the [pm-vibecode-ops](https://github.com/bdouble/pm-vibecode-ops) workflow:

- **`/codex-review [ticket-id]`** — Standalone cross-model review with Linear integration
- **`/execute-ticket [ticket-id]`** — Includes Codex review as Phase 5.5 (between Code Review and Security Review)
- **`/epic-swarm [epic-id]`** — Includes Codex review in each parallel ticket's workflow

### Without this MCP server

The pm-vibecode-ops workflow works fully without this server. `/execute-ticket` skips Phase 5.5 with a note, and `/codex-review` provides installation instructions.

## How It Works

```
Claude Code
    │ (MCP tool call over stdio)
    ▼
server.py (FastMCP)
    │
    ▼
codex exec --model gpt-5.3-codex -c reasoning.effort=xhigh --sandbox read-only
    │ (runs IN the project directory with full file access)
    │ (authenticates via CODEX_HOME/config.toml — ChatGPT subscription)
    ▼
Codex agent explores repo, reviews changes, applies fixes
    │
    ▼
Output captured via -o flag, returned to Claude Code
```

Codex runs in your actual repository, not against a diff blob. It can read any file, run git commands, trace code paths, and explore dependencies — identical to using the Codex UI manually.

The server never sees or stores your ChatGPT credentials. Authentication is handled entirely by Codex CLI.

## Troubleshooting

### "Codex CLI not found"

```bash
which codex        # check if installed
brew install codex-cli  # install
```

### "Codex CLI authentication failed"

```bash
codex login  # re-authenticate
```

### "Rate limit reached"

You've hit your subscription's rate limit (30-150 messages/5hr on Plus, 300-1500 on Pro). The workflow continues without the Codex review. Wait a few minutes and retry, or run `/codex-review` independently later.

To switch to a different subscription mid-session, edit `.env`:
```bash
CODEX_REVIEW_HOME=/path/to/other/.codex
```
The next tool call will use the new credentials (live reload).

### MCP server not appearing in Claude Code

```bash
# Check registration
claude mcp list

# Re-register
claude mcp remove codex-review-server
claude mcp add --scope user codex-review-server -- \
  ~/.claude/mcp/codex-review-server/.venv/bin/python3 \
  ~/.claude/mcp/codex-review-server/server.py
```

Start a **new** session — MCP servers load at session start.

### "No matching distribution found for mcp"

Python too old or not using the virtual environment:

```bash
~/.claude/mcp/codex-review-server/.venv/bin/python3 --version  # needs 3.10+
```

### Review times out

Repo-aware reviews with `xhigh` reasoning on large diffs can take 10-20 minutes. Increase the timeout:

```bash
# In .env
CODEX_REVIEW_TIMEOUT=2400  # 40 minutes
```

### Test components independently

```bash
# 1. Test Codex CLI
codex exec "Reply with just hello" --sandbox read-only --skip-git-repo-check --color never

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
