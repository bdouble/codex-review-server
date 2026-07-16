#!/usr/bin/env bash
# Launch the Codex delegation MCP server with a Python that has its deps.
#
# The server talks MCP over stdio, so stdout belongs to the protocol. Every
# diagnostic here goes to stderr — a stray echo would corrupt the stream and
# the server would fail to handshake with no obvious cause.
#
# Python resolution order:
#   1. $CODEX_MCP_PYTHON, if you want to point at a specific interpreter
#   2. the repo's .venv, which is what the documented manual install creates
#   3. bootstrap a .venv and install requirements (first run as a plugin)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
VENV_PY="$VENV/bin/python3"

log() { printf '[codex-delegate] %s\n' "$1" >&2; }

has_deps() {
  "$1" -c 'import mcp, dotenv' >/dev/null 2>&1
}

if [[ -n "${CODEX_MCP_PYTHON:-}" ]]; then
  PY="$CODEX_MCP_PYTHON"
  if ! has_deps "$PY"; then
    log "CODEX_MCP_PYTHON=$PY is missing deps. Install them with:"
    log "  $PY -m pip install -r $ROOT/requirements.txt"
    exit 1
  fi
elif [[ -x "$VENV_PY" ]] && has_deps "$VENV_PY"; then
  PY="$VENV_PY"
else
  BOOTSTRAP="$(command -v python3 || true)"
  if [[ -z "$BOOTSTRAP" ]]; then
    log "python3 not found on PATH. Install Python 3.10+ and retry."
    exit 1
  fi

  if [[ ! -x "$VENV_PY" ]]; then
    log "First run: creating a virtualenv at $VENV"
    "$BOOTSTRAP" -m venv "$VENV" >&2
  fi

  log "Installing dependencies (one time)…"
  "$VENV_PY" -m pip install -q --disable-pip-version-check \
    -r "$ROOT/requirements.txt" >&2

  if ! has_deps "$VENV_PY"; then
    log "Dependency install failed. Try manually:"
    log "  $VENV_PY -m pip install -r $ROOT/requirements.txt"
    exit 1
  fi
  PY="$VENV_PY"
  log "Ready."
fi

exec "$PY" "$ROOT/server.py"
