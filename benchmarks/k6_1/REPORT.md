# K.6.1 — CodeGraph Coverage & Context Optimization — Report

## Verdict: `IMPROVED`

Root causes proven by source-level evidence; fixes are minimal and
API-compatible; T2 consumer discovery and T6 method resolution both fixed;
T3 complete-slice behavior retained; every incompleteness is now explicitly
reported instead of silent. Remaining gaps honestly marked.

---

## 1. Root causes (evidence-backed)

| # | Symptom (from K.6) | Root cause | Class |
|---|---|---|---|
| R1 | T2 missing `extract_failures`/`verdict_for` | `closure()` walks `edges_from` only; reverse edges exist in data but no `edges_to` API | Traversal direction |
| R2 | T1 slice (−48%) bigger than Grep | `context_slicer.py:64` inlined `fact.source` (full body) instead of the already-computed stub | Emission policy |
| R3 | T4/T6 `UNKNOWN not indexed` | Methods indexed as `Class.method`; bare `run`/`from_dict` not symbol names | Caller contract |
| R4 | T4-rev small + empty | `from_dict` genuinely has no callers (constructor used elsewhere) — true negative, not a bug | — |

## 2. Changes implemented

- `asha/codegraph.py`: `edges_to`; `closure(reverse, max_nodes)`; `ClosureResult.budget_exhausted`.
- `asha/context_slicer.py`: `slice_context(reverse, max_nodes)`; stub emission (not full bodies); `completeness` + `completeness_reasons`.
- `tests/test_k61_codegraph.py`: 7 new regression tests.
- `benchmarks/k6_1/`: run harness, frozen spec, root cause, implementation, exact commands, results, manifest, report. (Frozen K.6 artifacts untouched.)

## 3. Regression tests — 7/7 + 18/18 existing

```
tests/test_k61_codegraph.py ........ 7 passed
tests/test_delta_check.py + test_k53_stale_pyc_regression.py 18 passed
ruff clean on both changed files
```

Coverage: reverse discovery, node budget, cycle termination, unresolved
boundary honesty, forward unchanged, stub compactness, both-ways T3.

## 4. Before / after (tokens, reduction vs Grep, completeness state)

| Task | Grep | K.6 CodeGraph (fwd) | K.6.1 fwd | K.6.1 rev(b50) | K.6.1 both | Best complete |
|---|---|---|---|---|---|---|
| T1 | 6968 | 10311 (−48%) | 10311 (−48%) ✓ | 3205 (+54%) **INCOMPLETE** | 13520 (−94%) ✓ | fwd (only complete) |
| T2 | 1413 | 186 (+86.8%) **missing 2** | 186 (+86.8%) **missing 2** | 791 (+44%) ✓ **FIXED** | 982 (+30.5%) ✓ | **rev/both** |
| T3 | 9271 | 1248 (+86.5%) ✓ | 1248 (+86.5%) ✓ | 3062 (+67%) **INCOMPLETE** | 4314 (+53.5%) ✓ | fwd (1248) |
| T4 | 1069 | UNKNOWN (bare name) | 184 (+82.8%) ✓ | 72 (+93.3%) **INCOMPLETE** | 260 (+75.7%) ✓ | fwd (184) |
| T5 | 7147 | 9700 (−35.7%) ✓ | 9700 (−35.7%) ✓ | 2350 (+67.1%) **INCOMPLETE** | 12054 (−68.7%) ✓ | fwd (9700) |
| T6 | 109721 | UNKNOWN (bare name) | 10588 (+90.4%) ✓ | 1608 (+98.5%) **INCOMPLETE** | 12200 (+88.9%) ✓ | **fwd (10588)** |

Highlight rows:

- **T2**: reverse traversal discovers the consumers that forward could not
  → `rev`/`both` complete. Acceptance criterion 1 met.
- **T6**: `DAGCoordinator.run` qualified resolution turns `UNKNOWN` into a
  **90.4% token reduction with complete context**.
- **T3**: complete-slice behavior retained unchanged (1248 tokens, 86.5%).
- **T1**: fwd remains larger than grep (the 170-line body problem); rev is
  54% smaller but budget-50 exhausts → honestly INCOMPLETE, not silent.

## 5. Completeness & uncertainty

- Every `INCOMPLETE` carries machine-readable reasons (budget exhausted /
  unresolved nodes / no deps). No false `COMPLETE`.
- `UNKNOWN` only at lookup failure (target not indexed / bare method name).
- T4-rev's emptiness is a TRUE NEGATIVE (no consumers exist for
  `from_dict`), verified against the graph.

## 6. Agent-in-the-loop

**NOT PERFORMED** — no LLM API key available in the container
(`NOT_PERFORMED: no API key`). Correctness conclusions remain
deterministic-only; no claim of improved real-agent performance.

## 7. Remaining limitations

1. T1-class slices (large target bodies) still lose to Grep when complete;
   reverse needs a bigger budget than 50 for `collect_worker_evidence`.
2. Method targets require qualified names (`Class.method`) — caller contract.
3. `max_nodes` is a single global cap; no per-depth budget.
4. Completeness is defined per declared slice policy, not repo-wide.
5. Stub serialization (docstrings + signature heads) still dominates for
   high-fan-out functions (136 stubs for T1).

## 8. Commit & reproducibility

- Commit: `PENDING` (not pushed; awaiting authorization).
- Branch: `main`, HEAD prior `778e2f9`.
- CI: queued after push.
- Reproduce: see `EXACT_COMMANDS.md`. Deterministic (no RNG/clock).
- Artifacts: `benchmarks/k6_1/{FROZEN_SPEC.md, ROOT_CAUSE.md,
  IMPLEMENTATION.md, EXACT_COMMANDS.md, run_k6_1.py, run_agent.py,
  results/results.json, results/agent.json, manifest.json, REPORT.md}`.

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | T2 consumers discovered or explicit incomplete | ✅ discovered (rev/both) |
| 2 | T1 overhead reduced without losing required context | ⚠️ rev −54% but budget-incomplete; fwd complete but −48% |
| 3 | T3 complete-slice retained | ✅ 1248 tokens, COMPLETE |
| 4 | No forward/public regression | ✅ 18/18 + 7/7; defaults unchanged |
| 5 | Tests: reverse, bounds, uncertainty, completeness | ✅ 7 tests |
| 6 | Versioned, hash-verified artifacts | ✅ manifest.json |
| 7 | No governance/test-exec change | ✅ 2 files only (graph + slicer) |
| 8 | Real-agent improvement claim | N/A — agent loop NOT run |

Criterion 2 is partial: reverse improves when budget allows; the honest
`INCOMPLETE` boundary is the deliverable. Overall: `IMPROVED`.