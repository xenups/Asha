# Hermes Disciplined Harness (Asha-Harness)

J-Space SV1 cooperative controller contract. This root SKILL.md is the file
set the `.jspace/control.py` read-gate verifies before any phase gate
(`check --stage work|ship`): the root agent must read this file plus the
active `modules/*` files with valid SHA-256 receipts.

Invariants:
- Every subcommand requires `--transport <ssh|local>` (fail-closed, exit 1).
- Atomic SEARCH/REPLACE patching only (`diff_engine.py`).
- AST-first exploration (`code_search.py --outline`).
- Ship gate: clean tree + scoped evidence (`.jspace/evidence.json`).

## Modules

- `modules/self-monitoring.md` (default / medium level)
- `modules/capacity.md`, `modules/broadcast.md`, `modules/orchestration.md`
  (high / xhigh levels, routed via `control.py route`)
