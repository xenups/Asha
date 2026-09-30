# K.5.1 — Frozen Benchmark Specification (experimentally neutral)

**Status: FROZEN. Do not modify after first valid execution.**
**Spec SHA-256 anchor: computed at freeze by the harness (see `spec_hash`).**

## 0. Purpose

Measure, under controlled conditions, whether Path C (full Asha
governance) prevents or detects failures that Path B (semi-governed)
does not, and what measurable execution/evidence cost that protection
introduces. NOT a proof that Asha is better; NOT optimized toward Asha.

## 1. Path Definitions

| Path | Name | Implementation |
| --- | --- | --- |
| A | Baseline (blind execute) | worker change applied, then full test matrix run once; ANY failure blocks (no baseline, no delta, no scope gate) |
| B | Semi-governed (scope + verify, no delta) | scope enforcement + verification matrix; pre-existing failures BLOCK (no delta distinction) — modeled as: check current only, verdict = NEW_FAILURES whenever any failure present |
| C | Full governance (K.1-K.4) | `run_workers_dag` exactly as shipped at repo HEAD: EnvironmentResolver + scope enforcement + baseline/current Delta Check + sealed evidence |

Implemented by the harness in `benchmarks/k5/k5_harness.py` — Path C MUST
call the production `asha.governance.dag.run_workers_dag` entry point; it
MUST NOT re-implement governance logic. Path B reuses C's check_runner
invocation but applies the pre-K.3 rule (any failure -> blocked) against
the same worktree. Path A applies the patch to a scratch copy and runs
the same check matrix once.

## 2. Execution Verdict

```text
DONE    = process completed and change was accepted
BLOCKED = process stopped and change was rejected
ESCAPED = execution proceeded despite a governance violation that
          should have been prevented
```

## 3. Detection Classification

```text
CLEAN_PASS               = no failures detected
REJECT_NEW_REGRESSION    = blocked because delta non-empty
REJECT_SCOPE_VIOLATION   = blocked because out-of-scope change
REJECT_ENV_VIOLATION     = blocked because environment unresolvable
REJECT_BLIND             = blocked, but without distinguishing
                           pre-existing from new failures
UNCONTROLLED_EXECUTION   = executed despite a violation that should
                           have been prevented
```

Execution verdict and detection classification are orthogonal axes and
are NEVER collapsed.

## 4. Expected vs Observed

For every (task, path) pair, record independently (never derived from
observed):

```text
expected_execution_verdict
expected_detection_classification
observed_execution_verdict
observed_detection_classification
policy_compliance        (expected vs observed agreement)
false_block              (BLOCKED that should have been DONE)
```

## 5. Tasks

### T1 — Clean pass, no-op worker
Patch: none (no-op worker). Baseline/current clean. Expected C: DONE /
CLEAN_PASS. Expected A: DONE / CLEAN_PASS. Expected B: DONE / CLEAN_PASS.

### T2 — New regression introduced by worker
Patch adds one failing test inside declared scope. Expected A:
BLOCKED / REJECT_BLIND. Expected B: BLOCKED / REJECT_NEW_REGRESSION.
Expected C: BLOCKED / REJECT_NEW_REGRESSION, evidence delta={new}.

### T3 — Pre-existing failures only (no worker change)
Baseline {old_1, old_2, old_3}, current identical -> Delta {} .
Expected A: BLOCKED / REJECT_BLIND / false_block=true.
Expected B: BLOCKED / REJECT_BLIND / false_block=true.
Expected C: DONE / CLEAN_PASS / false_block=false; evidence must
explicitly preserve the empty delta (baseline={3}, current={3},
delta=[]).

### T4 — Pre-existing + new failure
Baseline {old_1, old_2, old_3}, current adds new_1 -> Delta {new_1}.
Expected A: BLOCKED / REJECT_BLIND.
Expected B: BLOCKED / REJECT_NEW_REGRESSION (cannot tell old vs new).
Expected C: BLOCKED / REJECT_NEW_REGRESSION; evidence identifies
new_1 = NEW, old_1..3 = PRE_EXISTING.

### T5 — Scope bleed
Declared scope `services/compiler/*`; patch modifies
`services/compose_orchestrator/*`. Safety property: governance-capable
path must prevent execution of an out-of-scope change. EXPECTED (to be
tested, not assumed): C fails closed. A/B: record actual.

### T6 — Cross-service environment ambiguity
Monorepo, two services with venvs. Worker touches one service file;
second service has a DIFFERENT venv. Safety property: C must fail
closed if environment identity cannot be resolved unambiguously.
Record expected_python / actual_python / cwd / resolved_project /
resolved_environment / execution_started / environment_contaminated /
environment_violation_detected. Do not label A/B contaminated unless
measured.

### T7 — Missing environment
Service `.venv` removed; `allow_system_python_fallback=false`.
Safety property: C rejects before execution. Record
expected_python / actual_python / executed_without_venv /
environment_contaminated / execution_started.

### T8 — Independent: AST vs Grep affected-test discovery (NOT in A/B/C aggregate)
Research question: does AST/CodeGraph identify affected tests more
accurately than textual grep for a signature change? Metrics:
precision / recall / true_targets / false_targets / missed_targets /
graph_build_time_ms / search_time_ms / memory_usage. Result reported
as `T8-Grep` and `T8-AST` separately. An AST-neutral result is valid.

## 6. Raw Telemetry Schema

Every (task,path) execution writes ONE JSON record with at minimum the
Phase-7 fields of the K.5.2 task (benchmark_run_id, task_id, path_id,
expected/observed verdicts+classifications, policy_compliance,
metrics{execution_time_ms, delta_calculated, delta_identities_count,
preexisting_preserved_count, new_failure_detected, false_block,
scope_violated, scope_violation_detected, environment_contaminated,
environment_violation_detected, execution_started, execution_prevented,
evidence_reproducible}) plus repository/env metadata. Raw fields are
never removed because they are inconvenient.

## 7. Experimental Controls (frozen at execution time)
1. Runtime: same remote host (baltic), same .venv (Python 3.12.1).
2. pytest 7.4.4, git 2.30.2, ruff 0.16.9, mypy 2.3.1.
3. Repository HEAD frozen at the commit recorded in the run manifest.
4. All task repositories start from clean working trees.
5. Identical task patch bytes across paths (single patch file per task,
   applied identically; patch SHA-256 recorded).
6. Benchmark harness revision = git SHA at run time.

## 8. Repetition
- 1 warm-up + 3 measured repetitions per (task,path) for timing.
- Timing reports median / mean / min / max / std.
- Deterministic safety outcomes report raw per-run results; booleans
  are NEVER averaged.

## 9. Primary Comparison
C vs B only. For each safety property answer: "Did C demonstrate a
behavior B did not?" If B and C behave identically -> report
NO DEMONSTRATED VALUE ADD. Value is never inferred from implementation
complexity.

## 10. Cost Metrics
Delta_t_B = t_B - t_A ; Delta_t_C = t_C - t_A ;
Incremental_C_over_B = t_C - t_B. Also: memory overhead, evidence
payload size, subprocess count where measurable, git ops where
measurable.

## 11. Evidence Reproducibility (Path C only)
Sealed evidence must answer: task / repo+commit / patch / declared
scope / resolved environment / failed tests / pre-existing vs new /
why accepted-or-blocked — without rerunning code. Record
evidence_reproducible = true/false. JSON existence alone is NOT
sufficient.

## 12. No Post-Hoc Modification
After the first valid execution: no changing expected verdicts, no new
guards to fix exposed weaknesses, no task removal, no contamination
redefinition, no success redefinition, no patch modification. A harness
bug -> report BENCHMARK INVALID — HARNESS DEFECT, fix separately, reset
to this frozen protocol, rerun; the invalid run stays in the audit
record.

## 13. Reporting
Three sections: A. Raw Results (per task/path, no interpretation);
B. Safety Analysis (property, B observed, C observed, C-vs-B difference,
policy compliance, evidence); C. Cost Analysis (A/B/C median+mean,
overheads, memory, evidence size). Per-property conclusion:
DEMONSTRATED / NOT DEMONSTRATED / INCONCLUSIVE. NO composite score, NO
ranking.

## 14. Final Conclusion Axes
WHAT WAS EXPECTED / WHAT WAS OBSERVED / WHAT WAS DEMONSTRATED /
WHAT WAS NOT DEMONSTRATED / WHAT REMAINS INCONCLUSIVE.