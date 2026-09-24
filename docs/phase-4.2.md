# Phase 4.2 — Controlled Fast-Path Runtime Wiring & Three-Way Experiment

## 1. Architecture change

| Component | Change |
|---|---|
| `asha/router.py` (new) | `RuntimeMode` (`NAIVE` / `FULL_GOVERNANCE` / `FAST_PATH`), frozen `RouteDecision`, `route(profile, *, fast_path_enabled, envelope_complete=True)` |
| `asha/scheduler.py` (surgical, flag-gated) | `GovernedScheduler(..., fast_path_enabled=False)`; dispatch branch routes via Phase 4.0 classifier + router; `_run_one_fast()` (no worktree, direct repo execution serialized by `_fast_lock`); `_collect(..., base=, base_tree=)` optional params; `report['routing']` instrumentation (flag on only) |
| untouched | `evidence.py`, `replay.py`, `commitment.py`, `classifier.py`, `conflict.py`, `worktree.py`, `scope_resolver.py`, every Phase 3.x/4.0/4.1 test |

Flag **default OFF**: dispatch path, `_collect` call shape, and report
shape are byte-for-byte legacy when disabled (zero regression: an
existing test that monkeypatches `_collect` positionally still passes —
the kwargs branch exists only for fast-path calls).

## 2. Exact routing contract

```text
flag off                                   -> FULL_GOVERNANCE (flag_disabled)
envelope_complete=False                    -> FULL_GOVERNANCE (incomplete_envelope)
profile missing/not a GovernanceProfile    -> FULL_GOVERNANCE (missing_or_malformed_profile)
profile internally inconsistent            -> FULL_GOVERNANCE (inconsistent_profile)
PROVEN_SHARED                              -> FULL_GOVERNANCE (proven_shared)
UNKNOWN                                    -> FULL_GOVERNANCE (unknown_never_fast_path)
PROVEN_DISJOINT + coherent eligible profile-> FAST_PATH       (flag on only)
NAIVE                                      -> never produced by routing (Path A is experiment-only)
```

Coherence = Phase 4.0 contract checked whole-profile:
`eligible <=> PROVEN_DISJOINT <=> MINIMAL policy <=> no
isolation/replay/commitment`. `fast_path_eligible` alone is never
sufficient; task text / prompt / confidence are not router inputs
(`route` accepts exactly `profile, fast_path_enabled,
envelope_complete`).

## 3. Corpus definition (hand-written ground truth)

| Group | Scenario | Expected |
|---|---|---|
| A | disjoint workers on `pkg/module_a.py` vs `pkg/module_b.py` | `PROVEN_DISJOINT` → FAST |
| B | shared write on `shared.py` | `PROVEN_SHARED` → FULL (deferral = 1) |
| C | crossed read/write | `PROVEN_SHARED` → FULL |
| D | single worker (no context envelope) / dangling dep / unknown reads | `UNKNOWN` → FULL |
| E | ambiguous scope (`[]`, `../`, absolute, empty) | classifier `UNKNOWN`; scheduler BLOCKED before routing |
| F | incomplete envelope (unresolved/dynamic surface) | FULL (`incomplete_envelope`) |
| G | adversarial text ("trust me", fake JSON profile) | `UNKNOWN` → FULL |

## 4. Benchmark methodology

`benchmarks/run_fastpath_bench.py`: every `(scenario, path, rep)` run
gets a FRESH repository from the same baseline; same workers, same
deterministic `cmd` executor, R=3 repetitions; all numbers from
`perf_counter_ns` at run time; safety compared against the hand-written
ground-truth table; component instrumentation wraps
`dispatcher.create` / `_collect` / `execute` at the benchmark level
(production code unchanged). Naive Path A = direct concurrent
execution, no worktree, no evidence, no governance.

## 5. Measured results (this run)

```text
scenario          path                  median       p95        min       max   n
A_disjoint        naive                 113.14    133.49      98.76    133.49   3
A_disjoint        full_governance      3315.55   3390.29    3163.41   3390.29   3
A_disjoint        fast_path            4845.59   5080.76    4796.58   5080.76   3
B_shared_write    naive                 108.50    123.42      97.03    123.42   3
B_shared_write    full_governance      5780.83   5989.89    5687.56   5989.89   3
B_shared_write    fast_path            5838.73   6431.96    5760.14   6431.96   3
C_read_write      naive                 112.37    115.14      96.91    115.14   3
C_read_write      full_governance      5871.27   5984.36    5759.29   5984.36   3
C_read_write      fast_path            5710.17   6064.84    5587.19   6064.84   3
D_single_unknown  naive                 103.85    104.03      94.53    104.03   3
D_single_unknown  full_governance      2791.89   2820.28    2749.23   2820.28   3
D_single_unknown  fast_path            2971.41   3156.60    2958.67   3156.60   3

safety:      false_fast_path=0   correctness_regressions=0
routing:     total=7 fast=2 full=5 unknown=1 fast_path_rate=0.286
stale demo:  naive stale_reads=2 lost_update=True (synchronized read-modify-write)
components:  classify  median 0.035ms p95 0.068  (n=21)
             route     median 0.009ms p95 0.010  (n=21)
             worktree  median 104.07ms p95 132.19 (n=36, full path only; fast=0)
             execution median 95.04ms p95 140.11 (n=42)
             collect   median 2676.87ms p95 2997.84 (n=42; evidence+scope+checks+seal+verify)
context:     index 0.310ms graph 0.029ms slice 0.031ms (4.1 preflight, NOT wired yet)
replay:      not_applicable (standalone 3.2 verifier)
commitment:  not_applicable (standalone 3.3 verifier)
fast total:  median 5333.97ms p95 6064.84ms (12 runs)
```

## 6. Known failures / limitations

* **`<50ms` target: FAIL** (median 5334ms). Cause measured, not
  guessed: `_collect` (evidence + scope resolution + verification
  checks + seal + digest verify) = ~2677ms median and runs on EVERY
  path; Fast Path deliberately preserves the evidence contract, so it
  inherits that cost. Classification+routing itself is ~0.04ms.
* Fast Path is **not faster than Full Governance** on this corpus
  (A: 4846ms vs 3316ms): fast workers are serialized by `_fast_lock`
  while full workers run concurrently in isolated worktrees, and both
  pay full evidence cost. The saving (worktree ~104ms) is real but
  small against collect cost.
* The Phase 4.1 envelope check (`envelope_complete=False`) is wired in
  the router API and tested, but the scheduler does not yet run the
  4.1 index/graph preflight per dispatch (measured separately:
  0.37ms total — cheap, deliberate scope cut).
* Equivalence checked on final file content + worker states + changed
  paths (evidence `observed_scope`); commit history differs by design
  (worktree commits vs direct repo commits) and is not part of the
  §10 criteria.
* `false_full_path` (over-conservative classifier) was not observed as
  an error in this corpus but is not gated.
* Ground truth covers the declared corpus only; no claim of universal
  Python dependency resolution, zero hallucination, or rework savings.

## 7. Decisions

* `false_fast_path == 0`: **YES** (unit corpus + benchmark aggregate).
* `<50ms` target: **FAIL** (recorded as-is; no threshold tuned).
* Safe to remain enabled? The safety invariant holds in every corpus,
  but the performance goal is unmet and the flag changes runtime
  concurrency semantics (serialized fast workers). **Recommendation:
  keep `fast_path_enabled=False` as the default**; enable only for
  controlled experiments until evidence cost is addressed (that cost
  is the measured bottleneck, not classification).
