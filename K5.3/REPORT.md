# K.5.3 — Production Regression Detection: Root Cause + Fix Verification

**Repo commit:** 43a4b32 (pre-fix) → HEAD with fix
**Env:** Python 3.12.1 · pytest 7.4.4 · ruff 0.16.9 · mypy 2.3.1 · git 2.30.2
**K.5.2 results:** immutable (benchmarks/k5/results/, commit 43a4b32)
**Post-fix experiment:** K5.3/post_fix/ (k5-20260930-084609-all.json + t8-probe.json)

---

## 1. Root cause

The defect was caused by a **stale timestamped `.pyc` in the DAG worker worktree**:

1. The DAG's `_run_one` ran `baseline_checks` (pytest on the clean worktree), which writes `__pycache__/app.cpython-312.pyc` compiled from the **pre-change** source (`return 1`). The pyc header flags=0 (timestamp-based mode, NOT hash-based).
2. `run_worker_in_worktree` strips these caches after baseline (`_strip_check_caches`, line 331) — **but the DAG path (`run_workers_dag` → `_run_one`) never calls it**.
3. The worker rewrote `app.py` (`return 1 → 2`) and committed.
4. The current-pass pytest found the stale pyc; the rewritten source's mtime matched the pyc's mtime **within the same wall-clock second** (Python's 1-second pyc freshness granularity) → pytest **executed the pre-change bytecode** → `test_f` passed → `DONE/CLEAN_PASS` → the new regression was silently missed.
5. When the worker's write landed in a different second than the baseline's pyc creation, mtime differed → pytest recompiled → `FAILED/NEW_FAILURES`. **Alternation = second-boundary race.**

Proven: `PYTHONDONTWRITEBYTECODE=1` (no pyc ever written) → **8/8 FAILED/NEW_FAILURES** deterministic. With bytecode enabled → alternating DONE/FAILED. Captured evidence (all 12 probe runs): `pyc_flags=0`, `pyc_hash_matches_src=False` (stale bytecode), `same_second=True`, `src_ret2=True`.

## 2. First divergence

Not a preserve-flag divergence — a **worktree-cache lifetime divergence** between the two execution entry points:

- `run_worker_in_worktree` (worker_execution.py:266): strips caches post-baseline (line 331) → safe.
- `DAGCoordinator._run_one` (dag.py:~212): calls `baseline_checks` then **skips the strip** → the stale-pyc window.

The apparent `preserve_on_failure` dependence in K.5.2 was a correlation artifact: the benchmark's preserve=True runs happened outside the same-second window; a standalone 5-rep reproduction showed preserve=False and preserve=True alternate identically. The real independent variable is the wall-clock second boundary.

## 3. Evidence

- Pre-fix, T2-C standalone: `DONE/None/None` ↔ `FAILED/verification_failed:pytest/NEW_FAILURES` alternating across reps (both preserve modes identical).
- Worktree snapshot at `collect_worker_evidence` entry, DONE and FAILED runs alike: `entry_app='def f():\n    return 2\n'`, `entry_porcelain=' M app.py'`, `base_app='return 1'` — **identical inputs, different verdicts**.
- pyc probe (12 reps): every rep `pyc_flags=0` (timestamp mode), `hash_match=False`, `same_sec=True` — stale bytecode trusted on same-second.
- `PYTHONDONTWRITEBYTECODE=1`: 8/8 FAILED. No bytecode, no race.
- Post-fix: 8/8 FAILED/NEW_FAILURES per mode, deterministic.

## 4. Fix (minimal, 8 lines, dag.py)

`DAGCoordinator._run_one`: after `baseline_checks`, call `worker_execution._strip_check_caches(path)` — exactly mirroring `run_worker_in_worktree`'s existing post-baseline strip. One function call, no architecture change, no semantics change beyond removing the baseline's cache artifacts before the worker executes (the same policy the non-DAG path already had).

Invariant restored: regression detection no longer depends on preserve mode; the only preserve-dependent difference is post-failure evidence persistence (as designed).

## 5. Regression tests (tests/test_k53_stale_pyc_regression.py)

| Test | Scenario | Proves |
|---|---|---|
| `test_k53_t2_new_regression_detected_both_preserve_modes[False/True]` | baseline ∅, current {new}; worker rewrites app.py return 1→2 with ns-precision mtime colliding the baseline pyc | both preserve modes → BLOCKED/NEW_FAILURES |
| `test_k53_t4_preexisting_plus_new_detected_both_modes[False/True]` | baseline {old×3}, current {old×3, new} | both modes → BLOCKED/NEW_FAILURES, delta=[new], baseline preserved (3) |
| `test_k53_t3_preexisting_only_remains_clean_pass` | baseline {old×3} = current | DONE/PRE_EXISTING_ONLY, delta=∅ — no false regression |

**Red/green confirmed:** with the fix stashed, the suite deterministically fails 2 (T2[F]/T4[F] or [T]) across 3 cycles; with the fix, 5/5 passes every time. The forced ns-precision mtime collision makes the race reproducible instead of probabilistic.

## 6. Verification

```
focused (k53 + delta):  18 passed
ruff:                   All checks passed (dag.py + test file)
mypy:                   Success: no issues in 2 files
full suite:             743 passed, 4 skipped, 1 failed =
                        (pre-existing test_env_no_src_no_pythonpath, untouched)
T2-C (post-fix bench):  4/4 BLOCKED/REJECT_NEW_REGRESSION
T4-C (post-fix bench):  4/4 BLOCKED/REJECT_NEW_REGRESSION
T3 (post-fix bench):    4/4 DONE/CLEAN_PASS (empty delta preserved)
```

## 7. Benchmark comparison (frozen K.5.2 methodology, post-fix rerun)

| Case | K.5.2 Before | K.5.3 After | Expected | Status |
|---|---|---|---|---|
| T2-C preserve=False | DONE/CLEAN_PASS ✗ | BLOCKED/REJECT_NEW_REGRESSION ✓ | BLOCKED/NEW_REG | **fixed** |
| T2-C preserve=True | FAILED/NEW 1/1 | BLOCKED/REJECT_NEW_REGRESSION ✓ | BLOCKED/NEW_REG | **fixed** |
| T4-C preserve=False | DONE 2/4 ✗ | BLOCKED ✓ | BLOCKED/NEW_REG | **fixed** |
| T4-C preserve=True | FAILED (when it ran) | BLOCKED ✓ | BLOCKED/NEW_REG | **fixed** |
| T3 | DONE/CLEAN_PASS ✓ | DONE/CLEAN_PASS ✓ | DONE/CLEAN_PASS | unchanged |
| T5 | REJECT_SCOPE_VIOLATION ✓ | REJECT_SCOPE_VIOLATION ✓ | REJECT_SCOPE | unchanged |
| T7 | REJECT_ENV_VIOLATION ✓ | REJECT_ENV_VIOLATION ✓ | REJECT_ENV | unchanged |

Post-fix benchmark aggregate: **0 non-compliant across all 21 task/path combos** (was: T2-C 4/4 + T4-C 2/4 non-compliant).

Timing (post-fix median, 28 runs): A 617ms, B 580ms, C 1088ms (ΔC/B ≈ +508ms +88%; consistent with K.5.2's +67% within variance).

T8 (post-fix): grep 1.00/1.00, AST 1.00/1.00 — unchanged null result, per Phase-6 independence.

## 8. Remaining uncertainty

- **`preserve_on_failure=True` + pre-fix alternation details**: the exact timing distribution of the second-boundary race was not characterized beyond empirical alternation; not needed for the fix.
- **Other cache artifacts** (`.pytest_cache`, `.ruff_cache`, `.mypy_cache`) are stripped by the same call and never bit the mutation case, but no dedicated regression test forces their stale reuse (only `__pycache__` is forced). A future variant could force stale `.mypy_cache` reuse specifically.
- **`test_env_no_src_no_pythonpath`** (pre-existing, unrelated): root cause still unexamined — out of scope per HARD RULE 7.
- The fix relies on `_strip_check_caches` raising no error mid-strip (it uses `ignore_errors=True`); a pathological read-only cache dir is silently tolerated, which could still leave a stale artifact — unmeasured, low likelihood.