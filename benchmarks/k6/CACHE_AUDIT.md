# CACHE_AUDIT.md — Cache-Isolation Invariant Audit (K.6 Part 1)

**HEAD:** dc68802554f451c5f5954f260b6de052d3bad100
**Invariant:** every pipeline crossing `Baseline Execution → Source Mutation → Current Verification` must isolate/strip verification-affecting caches before mutation or current verification.

## Status: HOLDS (after K.5.3)

## Pipelines audited (full execution topology)

| # | Pipeline | Entrypoint | Baseline | Mutation | Current verification | Cache stripping | Owner | Evidence/verdict |
|---|---|---|---|---|---|---|---|---|
| 1 | **DAG (production)** | `run_workers_dag` (dag.py) → `DAGCoordinator._run_one` | `baseline_checks` (dag.py:212) | worker `execute` in worktree | `collect_worker_evidence` → `_run_checks` | **`_strip_check_caches(path)` after baseline (dag.py:223)** — added K.5.3 | `DAGCoordinator._run_one` | `collect_worker_evidence` seals; delta verdict |
| 2 | **CLI validate (production)** | `cli.py:174` → `run_worker_in_worktree` | `baseline_checks` (worker_execution.py:329) | worker `execute` in worktree | `collect_worker_evidence` | **`_strip_check_caches(path)` (worker_execution.py:331)** — pre-existing | `run_worker_in_worktree` | same |
| 3 | **MCP run (production)** | `mcp_server.py:1086` → `run_worker_in_worktree` | same as #2 | same | same | same as #2 (protected) | `run_worker_in_worktree` | same |
| 4 | **MCP apply (production)** | `mcp_server.py:684` → `run_workers_dag` | same as #1 | same | same | same as #1 (protected post-K.5.3) | `DAGCoordinator._run_one` | same |
| 5 | **Scheduler (legacy, test-only)** | `GovernedScheduler._run_one` (scheduler.py) | **NONE** (no baseline pass) | worker `execute` | `_collect` | **Category C — not applicable** (no baseline → no baseline pyc; worker's own artifacts are the mutated tree's own) | — | `_collect` seals |

## Invariant boundary classification

- **A — Already protected:** pipelines #1 (post-K.5.3), #2, #3, #4. All route current verification through the same `_run_checks`/`collect_worker_evidence` machinery after a mandatory post-baseline `_strip_check_caches`.
- **B — Missing protection:** **NONE.** (K.5.3 closed the only instance: DAG `_run_one` lacked the strip.)
- **C — Not applicable:** pipeline #5 (scheduler has no baseline execution, so the baseline→mutation→current stale-cache window cannot form). Also `_run_one_fast`/`_run_one` fast paths execute against the repo directly with no baseline — Category C.

## Cache mechanisms inventory

| cache | producer | consumer | survives baseline→mutation? | can stale current verification? | existing invalidation | required action |
|---|---|---|---|---|---|---|
| `__pycache__/*.pyc` (timestamp-mode) | baseline pytest | current pytest | YES (default; K.5.3 race) | YES — proven (K.5.3: stale pyc → missed regression) | `_strip_check_caches` post-baseline | DONE (K.5.3) |
| `.pytest_cache/` | baseline pytest | pytest | yes | weak (nodeid cache; not bytecode) | same strip | covered |
| `.ruff_cache/` | baseline ruff | current ruff | yes | weak (ruff rescans; cache is perf-only) | same strip | covered |
| `.mypy_cache/` | baseline mypy | current mypy | yes | weak (mypy re-checks; cache is incremental) | same strip | covered |
| check_runner caches | none | — | — | — | — | none exist (verified grep) |
| env_resolver caches | none | — | — | — | — | none exist (verified grep) |

Only `__pycache__` pyc is a **strong** staleness vector (bytecode reuse). The others are perf caches; stripping them is correct hygiene, not correctness-critical.

## Fixes applied in this phase

**None.** K.5.3 (commit dc68802, `dag.py:223`) already applied the minimal fix; the audit confirms it completed the invariant. No duplicate policy was needed: `_strip_check_caches` is a single shared function owned by `worker_execution`, invoked by both orchestrator paths.

## Regression tests

- K.5.3 suite `tests/test_k53_stale_pyc_regression.py` (5 tests) covers pipeline #1/#4 (DAG) with forced deterministic mtime collision: baseline → mutation → current verification, both preserve modes.
- Test-only scheduler path has no baseline → no test needed (Category C).

## Verification

```
targeted (k53 suite):  5 passed
ruff:                  All checks passed
mypy:                  Success
full suite:            743 passed, 4 skipped, 1 pre-existing failure
                       (test_env_no_src_no_pythonpath — unrelated, untouched)
```

## Pre-existing failures

`tests/test_env_resolver.py::test_env_no_src_no_pythonpath` — fails on clean HEAD before this phase; unrelated to cache isolation (env var assertion, not cache). Not touched per K.6 rule 4/global rule 7.