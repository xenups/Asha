# K.5.2 — Controlled Benchmark Execution Report

**Run ID:** k5-20260930-074312
**Frozen spec:** cf0eab9e2099b62a98fba1d8465a6e36168b08b3ced8925f3a127321bfbaf6ed (benchmarks/k5/FROZEN_SPEC.md)
**Harness:** 01cdaaf02e38c20fdb5c37eff8a7b6843d07a62d3e421a0c52ce4019c510d6d8 (k5_harness.py)
**Runner:** ad4453c3ab5eeeb5a7a8d0a43f557cb281a47301d9356dbb430f7f211ab6fa50 (run_k5.py)
**T8 probe:** 9f9a72e37b4cdd372e344edbf28c623028ee2125c6258bd6cfb59bff46837a5e (run_t8.py)
**Repo commit:** 785493f0fdb21507f7eb16d62cca6567e52eeeec
**Env:** Python 3.12.1 · pytest 7.4.4 · ruff 0.16.9 · mypy 2.3.1 · git 2.30.2 (host baltic.void-star.co)

**Protocol:** 1 warm-up + 3 measured reps per task/path. Tasks T1–T7 × Paths A/B/C. T8 excluded from the aggregate (independent probe). All A/B/C executions use the PRODUCTION worker-execution/check-runner/governance code; Path C = `asha.governance.dag.run_workers_dag(..., preserve_on_failure=False)` — the production default.

---

## A. Raw Results (per-task/per-path; compliance matrix of the 4 runs: warmup + 3 reps)

| Task | Path A | Path B | Path C |
|---|---|---|---|
| T1 clean no-op change | ✓ 4/4 | ✓ 4/4 | ✓ 4/4 |
| T2 new regression (runtime) | ✓ 4/4 (BLOCKED/REJECT_BLIND) | ✓ 4/4 (BLOCKED/REJECT_NEW_REGRESSION) | **✗ 0/4 — ALL runs DONE/CLEAN_PASS (silent miss, deterministic)** |
| T3 pre-existing only | ✓ 4/4 | ✓ 4/4 | ✓ 4/4 |
| T4 pre-existing + new | ✓ 4/4 | ✓ 4/4 | **✗ 2/4 — 2× DONE/CLEAN_PASS (silent miss)** |
| T5 scope bleed | ✓ 4/4 | ✓ 4/4 | ✓ 4/4 (BLOCKED/REJECT_SCOPE_VIOLATION) |
| T6 env ambiguity | ✓ 4/4 | ✓ 4/4 | ✓ 4/4 |
| T7 missing venv | ✓ 4/4 | ✓ 4/4 | ✓ 4/4 (BLOCKED/REJECT_ENV_VIOLATION) |

Raw JSON per run: `benchmarks/k5/results/k5-20260930-074312-<TASK>-<PATH>-<0..3>.json`; consolidated: `k5-20260930-074312-all.json`. Environment manifest: `run-manifest.json`.

---

## B. Safety Analysis

### Scope (T5)
| | B | C |
|---|---|---|
| observed | DONE/CLEAN_PASS (allowed out-of-scope execution) | BLOCKED/REJECT_SCOPE_VIOLATION (prevented) |
| C vs B | **C prevented; B allowed.** DIFFERENCE DEMONSTRATED (4/4 runs) |

### Environment (T6 ambiguity, T7 missing venv)
| | B | C |
|---|---|---|
| T6 | DONE/CLEAN_PASS | DONE/CLEAN_PASS (resolved service venv; env unambiguous) |
| T7 | DONE/CLEAN_PASS (**executed without venv despite fallback disabled**) | BLOCKED/REJECT_ENV_VIOLATION (rejected before execution, 4/4) |
| C vs B | **C rejects when env identity is missing; B does not.** DIFFERENCE DEMONSTRATED (T7, 4/4) |

### Evidence & delta (T3/T4)
| | B | C |
|---|---|---|
| T3 | DONE/CLEAN_PASS (false-block, no delta) | DONE/CLEAN_PASS, delta=∅ explicitly preserved in sealed evidence (4/4) |
| T4 | BLOCKED/REJECT_NEW_REGRESSION (cannot distinguish old vs new) | BLOCKED/REJECT_NEW_REGRESSION (2/4); **DONE/CLEAN_PASS (2/4 — silent miss)** |
| evidence | none sealed | sealed when blocked; `delta_failures=[new]`, baseline/current identified |

### New-regression detection (T2) — **primary defect**
| | B | C |
|---|---|---|
| expected | BLOCKED/REJECT_NEW_REGRESSION | BLOCKED/REJECT_NEW_REGRESSION |
| observed | BLOCKED/REJECT_NEW_REGRESSION ✓ (4/4) | **DONE/CLEAN_PASS (4/4)** — regression silently accepted |
| C vs B | **C deterministically fails to detect a new regression on the production default path; B always blocks.** NEGATIVE DEMONSTRATION |

Root-cause reproduction (standalone, same repo+worker): `run_workers_dag(preserve_on_failure=False)` → DONE; `preserve_on_failure=True` → FAILED/NEW_FAILURES. The default path's worker-change capture/verify ordering is `preserve`-dependent. This is a genuine Asha defect surfaced by the benchmark; the benchmark harness did not cause it (identical input, only the production API flag differs).

---

## C. Cost Analysis (ms; 28 runs/path)

| | median | mean | min | max | sd |
|---|---|---|---|---|---|
| A | 695 | 698 | 556 | 836 | 55 |
| B | 691 | 1214 | 49 | 3048 | 1093 |
| C | 1152 | 1611 | 99 | 3640 | 1286 |

Overhead:
- ΔB/A = −4 ms median (not meaningful; B's fast scope-reject skews min)
- ΔC/A = +457 ms median (+66%)
- **ΔC/B = +461 ms median (+67%)** ← incremental cost of full governance over semi-governed

Memory: not instrumented per-run (in-process harness). Evidence payload size + subprocess/git counts: in raw JSON (`evidence_path`, `execution_time_ms`).

---

## T8 — AST vs Grep (independent probe)

| | precision | recall | hits | missed | time |
|---|---|---|---|---|---|
| Grep | 1.00 | 1.00 | test_render, test_other | ∅ | 4.5 ms |
| AST | 1.00 | 1.00 | test_render, test_other | ∅ | 6.2 ms |

**NO DEMONSTRATED ADVANTAGE for AST on this probe** (valid null result per spec Phase 6; graph_build_time_ms recorded 0.0 in JSON since no persistent graph was built — an in-memory AST walk was measured).

---

## Deliverables Check
1. Frozen K.5.1 spec/hash → `benchmarks/k5/FROZEN_SPEC.md` cf0eab9e… ✓
2. Harness revision → 01cdaaf0… ✓
3. Repo baseline commit → 785493f ✓
4. Patch hashes → per-record `patch_sha256` in raw JSON ✓
5. Tool/runtime versions → run-manifest.json ✓
6. Raw JSON results → 84 records ✓
7. Aggregated results → this document ✓
8. T8 independent results → t8-probe.json ✓
9. Reproducibility validation → Path C seals evidence (see raw `evidence_path`); blocked-path seals carry delta identity ✓
10. C-vs-B analysis → Sections B/C ✓
11. Invalid runs → **none in the authoritative run** (harness defects were fixed and the benchmark fully re-run before this run; the pre-fix runs were discarded, per spec Phase 13)
12. Exact commands → `benchmarks/k5/EXACT_COMMANDS.md` ✓

---

## Final Conclusion (expected vs observed vs demonstrated)

**WHAT WAS EXPECTED**
- C blocks regressions (T2/T4), false-blocks nothing (T3), rejects scope/env violations (T5/T7), costs more than B.

**WHAT WAS OBSERVED**
- T2-C: DONE/CLEAN_PASS in **4/4** runs (deterministic silent acceptance of a new regression) on the production default path. T4-C: silent miss in 2/4. T3-C, T5-C, T6-C, T7-C: compliant 4/4.

**WHAT WAS DEMONSTRATED**
- Scope: C prevents what B allows. ✓ (DEMONSTRATED, 4/4)
- Environment: C rejects missing-venv execution (T7) where B executes. ✓ (DEMONSTRATED, 4/4)
- Delta evidence: C distinguishes pre-existing vs new (T4 when it blocks) and seals empty-delta (T3). ✓ (DEMONSTRATED when C blocks)
- Cost: C = +67% median over B. ✓ (DEMONSTRATED)

**WHAT WAS NOT DEMONSTRATED**
- Reliable new-regression detection on the C default path: **NOT DEMONSTRATED — the default path deterministically misses it** (T2-C 4/4 miss, T4-C 2/4 miss). Root cause: `preserve_on_failure`-dependent ordering in the worker-change capture/verify path (reproduced standalone).

**WHAT REMAINS INCONCLUSIVE**
- The exact mechanism of the preserve-dependent divergence (worktree commit vs verify ordering) — the fix and a clean re-run belong to a follow-up phase. Until fixed, **Path C on its production default does not provide the new-regression safety property** for runtime changes; the delta machinery works (evidence shows correct verdicts when it does run) but the default path does not reliably reach it.

---

**Per spec Phase 13:** no post-hoc modification was made to the protocol after the authoritative run. All T2-C/T4-C non-compliances are preserved in raw JSON and reported, not hidden. The harness was cleaned (lint), re-verified, and the authoritative run regenerated against the final harness revision so the manifest hashes match executed code.