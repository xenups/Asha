#!/usr/bin/env bash
# Asha-Harness update wrapper (Linux / macOS).
# Usage: bash scripts/update.sh [--dry-run]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="python3"
if [ -x "$REPO_ROOT/.hermes/venv/bin/python" ]; then
  PY="$REPO_ROOT/.hermes/venv/bin/python"
fi

exec "$PY" "$REPO_ROOT/scripts/update.py" "$@"