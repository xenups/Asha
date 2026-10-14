# Asha N+1 — Integrity & Prove (revision 2) — Phase 1–3 Report

## 0. Outcome Summary

| Deliverable | Result |
|---|---|
| Unified baseline commit | **e5b8a69** (4 commits over d8d22f9) — both trees reconciled at it |
| D.1 empty-diff guardrail | Committed (d7b04ba) + regression-tested; verified live on single-commit repo |
| CI collection-truth hardening | Committed (93a6a8a): `collected == 0` → hard RED |
| Red-on-planted proof | `PYTEST_EXIT=1`, `1 failed, 751 passed, 4 skipped` |
| Green-on-revert proof | `PYTEST_EXIT=0`, `751 passed, 4 skipped` |
| Cross-platform fix found by reconciliation | e5b8a69 (`as_posix` hermetic path prefix) |

## 1. Phase 1 — Reconcile Divergent Checkouts

### Divergence (measured, not assumed)
- **Windows** `origin/main` baseline was **c9836c3** (12 commits behind baltic).
- **Baltic** `main` = d8d22f9 + **uncommitted** check_runner fix (`_env_argv` `-m` marker bug, new `_exec_path_args`, `tests/test_exec_path_args.py`, `tests/test_execution_coverage.py` edit).
- Windows' 16 uncommitted files: **14 byte-identical to baltic**, 2 stale-superseded (`scope_resolver.py`, `test_watcher_stepper.py`) — no unique work lost.

### Actions
1. Baltic's uncommitted fix committed as 2d76802 (19 tests green).
2. Windows tree: stale mods → `git stash` (`asha-windows-stale-pre-reconcile`, recoverable); untracked `PHASE_J.4_REPORT.md` preserved to Desktop + Temp (only copy, not in git anywhere); reset to origin/main.
3. Windows fast-forwarded to baltic line → **clean at e5b8a69 on both trees**.
4. **Cross-platform catch:** new `_exec_path_args` failed on Windows (`services\service_a` vs `services/service_a` prefix mismatch → false `CheckRunnerError`). Fixed with `as_posix()` normalization → e5b8a69. Windows exec_path tests: 8/8 pass. (One pre-existing Windows env-var failure `test_resolution_does_not_mutate_parent_env` confirmed failing at d8d22f9 baseline too — unrelated.)

### Baseline
```
e5b8a69 fix(check_runner): normalize hermetic exec path prefix with as_posix
93a6a8a ci: assert collected count > 0 -- empty suite must fail RED
d7b04ba fix(scope): D.1 empty-diff guardrail -- unresolved base + empty diff elevates to S2
2d76802 fix(check_runner): drop -m marker for direct pytest launcher; translate hermetic exec paths
d8d22f9 docs: add project orientation and agent governance guide   <- origin/main
```
Not pushed to origin/main — user merge workflow.

## 2. Phase 2 — CI Verification & Falsification

### ci.yml hardening (93a6a8a)
- `pytest tests/ -v` step now: captures exit code, tails log, asserts `collected > 0`.
- pytest exit 5 (no tests collected) → **RED**, not green. Runner-wrappers masking exit codes cannot hide an empty suite.

### Red-on-planted (scratch worktree at d8d22f9)
Planted `tests/test_planted_regression.py` (`assert 1 == 2`). Full CI suite:
```
PYTEST_EXIT=1
1 failed, 751 passed, 4 skipped
FAILED tests/test_planted_regression.py::test_deliberately_false - AssertionError: planted regression: CI must go RED
```
### Green-on-revert
Reverted the plant:
```
PYTEST_EXIT=0
751 passed, 4 skipped, 471 warnings in 413.70s
```
Logs preserved: `/tmp/red_proof.log`, `/tmp/green_proof.log` (baltic).

### Count-assert guard, both paths
- Normal: `collected>0` → GREEN.
- Empty dir: `pytest_exit=5 collected=0` → RED-empty (guard fires).

## 3. Phase 3 — D.1 Guardrail + Re-verification

### Guardrail (d7b04ba, `scope_resolver.py`)
`resolve()` with `base is None` AND empty path set previously returned **S0/pytest-only** — a vacuous seal-on-nothing (single-commit repo / shallow clone / no upstream). Now elevates to **S2 (ruff+pytest+mypy)**, mirroring `ship_gate` restoration:
```python
if not paths:
    if base is None:
        # D.1 empty-diff guardrail: ... Elevate to the full S2 matrix
        scope = 'S2'
        reasons.append('unresolved base + empty diff -> full S2 (D.1)')
    else:
        scope = 'S0'
        reasons.append('no changes detected')
```
Verified live: single-commit repo → S2 + all 3 checks + D.1 reason; with-base empty diff → S0 (no false elevation). Regression tests: `tests/test_d1_guardrail.py` (2 tests; 21/21 exec+scope tests green both trees).

### Historical dataset re-verification
- **k6** (`benchmarks/k6/results/`, `repo_commit: dc68802`): harness measures `context_slicer` retrieval, never the `resolve()` vacuous path; strategies A1/A2/B1/B2 all grounded on real committed repos with symbol-level ground truth — not seal-on-nothing. Numbers stand; guard does not alter their semantics.
- **live_eval** (`benchmarks/live_eval/AB_RESULTS.md`): explicitly "no fabricated data", no LLM-in-loop, per-trial concrete corpora — grounded. Stands.
- The seal-on-nothing hole the guard closes was a *governance dispatch* hole (fresh/shallow/no-upstream repos), not a hole in either benchmark's corpus — datasets were grounded before and after.

## 4. Open Items / Deferred
- Pushing e5b8a69 lineage to origin/main (user merge workflow).
- `PHASE_J.4_REPORT.md` never committed upstream — preserved locally, needs a home.
- Pre-existing Windows env-var test failure (unrelated, at d8d22f9 too).
- Stash `asha-windows-stale-pre-reconcile` kept for 30 days, then droppable.