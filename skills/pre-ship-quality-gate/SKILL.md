---
description: "Pre-ship critique gate: cleanup, races, exit 0."
name: pre-ship-quality-gate
---

# PRE-SHIP QUALITY GATE: DRAFT & CRITIQUE (FAIL-CLOSED)

Mandatory before committing, pushing, or declaring any patch complete (pre-ship phase in J-Space).

## 1. Adversarial Inspection
Review the candidate diff assuming strict production load. Explicitly evaluate:
- **Resource cleanup** — are async sessions, sockets, or file handles closed? Context managers used?
- **Race conditions / shared mutable state** — under asyncio concurrency: shared lists/dicts mutated across tasks, non-atomic check-then-act, missing locks/semaphores.
- **Unhandled exception propagation** — bare `except Exception` leaks, swallowed validation errors, exceptions that should propagate but are caught.
- **Input validation at trust boundaries** — pydantic/type guards on external input.
- **Type mismatches** — wrong types in collections, `Optional` misuse, `Any` leaks.

## 2. Critique Verdict
If any risk is detected, **patch it immediately** before running gates. Never ship a known risk.

## 3. Gate Assertion
Ship ONLY when critique passes AND linter/type tests return exit code 0:
- `ruff check .` (or project linter)
- `mypy .` if project uses it
- `pytest` (full relevant suite)
- J-Space `control.py --transport <ssh|local> check --stage ship` if loop-mode controller active.

## 4. Tooling
- Structural review aid: `code_search.py --outline <file>` (AST symbol map, no bodies).
- Precision edits: `diff_engine.py --file X --patch "..."` (atomic SEARCH/REPLACE).
- Heuristic persistence: `memory_bridge.py --store CATEGORY PATTERN SOLUTION` after any non-trivial fix.
- Sequential/structured reasoning: `sequentialthinking` MCP tool for multi-step review.

Fail-closed: if any gate is red or a risk is unresolved, the patch is NOT shippable. Say so plainly.
