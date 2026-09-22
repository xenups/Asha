#!/usr/bin/env bash
# Asha-Harness one-shot bootstrap (Linux / macOS).
# Idempotent: safe to re-run. Fail-closed: any failed step aborts with exit 1.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV=".venv"
MCP_FLAG="${ASHA_SKIP_MCP:-0}"
MCP_LOG="$(mktemp 2>/dev/null || echo "$REPO_ROOT/.jspace/mcp-bootstrap.log")"

echo "[asha] root: $REPO_ROOT"
echo "[asha] venv: $VENV"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[asha] FATAL: python3 not found on PATH" >&2
  exit 1
fi

# --- 1. Isolated virtual environment -------------------------------------
if [ -x "$VENV/bin/python" ]; then
  echo "[asha] venv exists; keeping it"
else
  echo "[asha] creating venv"
  python3 -m venv "$VENV"
fi
PY="$REPO_ROOT/$VENV/bin/python"

# --- 2. Pinned ABI deps + toolchain --------------------------------------
echo "[asha] installing pinned toolchain (tree-sitter 0.21.3 / 1.10.2 / ast-grep-py 0.45.3, ruff, mypy, pytest)"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet \
  "tree-sitter==0.21.3" \
  "tree-sitter-languages==1.10.2" \
  "ast-grep-py==0.45.3" \
  "ruff" \
  "mypy" \
  "pytest" \
  "chromadb" \
  "mem0ai"

# --- 3. Fail-closed pin verification --------------------------------------
echo "[asha] verifying pinned ABI matrix"
"$PY" .hermes/tools/code_search.py --verify-env || {
  echo "[asha] FATAL: --verify-env failed; toolchain is not the pinned matrix" >&2
  exit 1
}
"$PY" .hermes/tools/code_search.py --self-test
"$PY" .hermes/tools/diff_engine.py --self-test

# --- 4. MCP servers over stdio (optional, skip with ASHA_SKIP_MCP=1) ------
if [ "$MCP_FLAG" = "1" ]; then
  echo "[asha] skipping MCP registration (ASHA_SKIP_MCP=1)"
else
  echo "[asha] registering MCP servers over stdio via Hermes CLI"
  command -v hermes >/dev/null 2>&1 || {
    echo "[asha] WARNING: hermes CLI not on PATH; MCP registration skipped (re-run after installing Hermes)" >&2
  }
  if command -v hermes >/dev/null 2>&1; then
    hermes mcp add sequential_thinking \
      --command npx \
      --args "-y" --args "@modelcontextprotocol/server-sequential-thinking" \
      --connect-timeout 30 >>"$MCP_LOG" 2>&1 || \
      echo "[asha] WARNING: sequential_thinking add failed (pre-registered?) — see $MCP_LOG" >&2
    hermes mcp add remote_linux \
      --command npx \
      --args "-y" --args "mcp-server-ssh" \
      --connect-timeout 30 >>"$MCP_LOG" 2>&1 || \
      echo "[asha] WARNING: remote_linux add failed (pre-registered?) — see $MCP_LOG" >&2
    echo "[asha] MCP servers registered; verify with: hermes mcp list && hermes mcp test <name>"
    rm -f "$MCP_LOG"
  fi
fi

# --- 5. Gate assertion ----------------------------------------------------
echo "[asha] running gates"
"$PY" -m ruff check . >/dev/null
"$PY" -m mypy .hermes/tools/ .jspace/control.py tests/ >/dev/null
"$PY" -m pytest tests/ -q >/dev/null

echo "[asha] BOOTSTRAP OK: venv=$VENV, pins verified, gates green"
echo "[asha] next: python .jspace/control.py --transport <ssh|local> init --goal '<G>' --next '<N>'"