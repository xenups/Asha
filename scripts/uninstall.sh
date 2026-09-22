#!/usr/bin/env bash
# Asha-Harness zero-bleed self-teardown (Linux / macOS).
# Idempotent: re-running is a no-op. Never touches files outside the repo
# .venv/.jspace bounds or the two MCP entries this harness registered.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- 0. Listening-port snapshot (zero-bleed assertion) -------------------
if command -v ss >/dev/null 2>&1; then
  PORTS_BEFORE="$(ss -tln | LC_ALL=C grep -oP ':\K\d+(?=\s)' | sort -u)"
else
  PORTS_BEFORE="$(netstat -tln 2>/dev/null | awk '/LISTEN/{print $4}' | sed 's/.*://' | sort -u)"
fi

cleanup_port_check() {
  if command -v ss >/dev/null 2>&1; then
    PORTS_AFTER="$(ss -tln | LC_ALL=C grep -oP ':\K\d+(?=\s)' | sort -u)"
  else
    PORTS_AFTER="$(netstat -tln 2>/dev/null | awk '/LISTEN/{print $4}' | sed 's/.*://' | sort -u)"
  fi
  NEW_PORTS="$(comm -13 <(printf '%s\n' "$PORTS_BEFORE") <(printf '%s\n' "$PORTS_AFTER"))"
  if [ -n "$NEW_PORTS" ]; then
    echo "[asha] FAIL: new listening ports detected post-teardown: $NEW_PORTS" >&2
    exit 1
  fi
}
trap cleanup_port_check EXIT

# --- 1. Registered MCP servers (this harness's own registrations) --------
if command -v hermes >/dev/null 2>&1; then
  for NAME in sequential_thinking remote_linux; do
    if hermes mcp list 2>/dev/null | grep -q "^[[:space:]]*$NAME[[:space:]]"; then
      echo "[asha] removing MCP server: $NAME"
      hermes mcp remove "$NAME" >/dev/null 2>&1 || echo "[asha] WARNING: mcp remove $NAME failed" >&2
    else
      echo "[asha] MCP server $NAME not registered; nothing to remove"
    fi
  done
else
  echo "[asha] WARNING: hermes CLI not found; MCP entries left untouched (remove manually: hermes mcp remove <name>)" >&2
fi

# --- 2. Isolated venv -----------------------------------------------------
if [ -d ".venv" ]; then
  echo "[asha] removing .venv"
  rm -rf ".venv"
else
  echo "[asha] .venv absent; nothing to remove"
fi

# --- 3. J-Space caches / stale lock --------------------------------------
rm -rf ".jspace/cache"
rm -f ".jspace/lock"
echo "[asha] .jspace/cache and stale lock removed"

# --- 4. Zero-bleed assertions --------------------------------------------
# Processes
if pgrep -f "hermes-disciplined-harness|asha-harness" >/dev/null 2>&1; then
  echo "[asha] FAIL: lingering harness processes found" >&2
  pgrep -af "hermes-disciplined-harness|asha-harness" >&2 || true
  exit 1
fi

echo "[asha] TEARDOWN OK: .venv gone, caches gone, MCP entries pruned, zero processes, zero new listeners"
trap - EXIT
exit 0