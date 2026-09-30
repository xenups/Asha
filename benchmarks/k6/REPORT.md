# K.6 — Cache-Isolation Audit + Context/Token Reduction Benchmark Report

**HEAD:** dc68802554f451c5f5954f260b6de052d3bad100
**Spec:** c39cb9c405f1ffd4 (benchmarks/k6/FROZEN_SPEC.md)
**Harness:** 1be5c30f30e7834c (benchmarks/k6/k6_harness.py)
**Env:** Python 3.12.1 · tiktoken 0.14.0 (cl100k_base) · git 2.30.2

---

# PART 1 — Cache-Isolation Invariant Audit

| Field | Value |
|---|---|
| Invariant | pipelines crossing Baseline→Mutation→Current-Verification must isolate verification-affecting caches |
| Status | **HOLDS** (no new defect; K.5.3 already closed the only gap) |
| Pipelines audited | 5 (DAG, CLI validate, MCP run, MCP apply, legacy Scheduler) |
| Protected | 4 (DAG + CLI + MCP run + MCP apply — all route through post-baseline `_strip_check_caches`) |
| Unprotected | 0 |
| Not applicable | scheduler (no baseline pass → no baseline cache window) |
| Fixes | none needed this phase |
| Regression tests | K.5.3 suite (5 tests, tests/test_k53_stale_pyc_regression.py) already covers the DAG path; no new path to cover |

Details: see `benchmarks/k6/CACHE_AUDIT.md` (full topology table, cache inventory,
boundary classification A/B/C).

---

# PART 2 — Context/Token Reduction Benchmark

## Research question
Does AST/CodeGraph dependency slicing give the agent the context needed for a task with
fewer tokens than Grep/full-file retrieval, without losing required information?

## Tasks (frozen, Asha repo itself)
- **T1** — Signature refactor: `collect_worker_evidence` (+ required kw-only param, find call sites)
- **T2** — Data model change: `FailureIdentity` (+ field, find consumers)
- **T3** — Workflow change: `default_execute` dispatch path (+ pre-execution barrier)

## Strategies
A1 = grep symbol → ±50-line windows (primary baseline)
A2 = grep symbol → whole files
B1 = CodeGraph slice, 1-hop (primary AST strategy)
B2 = CodeGraph slice, 2-hop
(2-hop produced identical slices to 1-hop in all cases — BFS reached the same closure;
recorded as a property, not a bug.)

## Tokenizer
tiktoken `cl100k_base` v0.14.0 (frozen). Grep fallback = stdlib line scan (rg binary not
present in the container; same retrieval semantics, noted in harness).

## Results (raw: results/k6_results.json)

| Task | Strategy | Tokens | Red vs A1 | Noise | Complete | Missing |
|---|---|---:|---:|---:|---|---|
| T1 | A1 | 6968 | — | 0.99 | ✓ | — |
| T1 | A2 | 11606 | −66.6% | 0.99 | ✓ | — |
| T1 | B1 | 10311 | **−48.0%** | 0.98 | ✓ | — |
| T2 | A1 | 1413 | — | 0.96 | ✓ | — |
| T2 | A2 | 1149 | +18.7% | 0.96 | ✓ | — |
| T2 | B1 | 186 | **+86.8%** | 0.98 | **✗** | extract_failures, verdict_for |
| T3 | A1 | 9271 | — | 0.99 | ✓ | — |
| T3 | A2 | 23122 | −149.4% | 1.00 | ✓ | — |
| T3 | B1 | 1248 | **+86.5%** | 0.93 | ✓ | — |

## Analysis

1. **T3 (dispatch/workflow) — AST wins decisively:** 1248 vs 9271 tokens (−86.5%),
   complete. The slice of `default_execute`'s callee closure is compact and sufficient.
2. **T2 (leaf dataclass) — AST loses on completeness:** 186 tokens (−86.8%) but the
   downstream slice cannot reach `extract_failures`/`verdict_for` — the CONSUMERS are
   **callers**, and the slice is dependency-down only. **Graph limitation recorded as
   UNKNOWN for reverse/caller discovery** (spec K.6.4). Grep finds the consumers; AST
   cannot.
3. **T1 (170-line function) — AST backfires:** slice = target body (170 lines) + every
   internal reference stub → 10311 tokens, **larger than grep's 6968**. The slice is only
   as compact as the target's own body.
4. **Noise metric:** uniformly high (0.93–1.00) even for A1 because relevance scoring is
   line-level-crude (def/call-line matching); reported with the symbol-level ground-truth
   limitation per K.6.7. Noise does not differentiate strategies meaningfully here and is
   NOT used as a decision metric.

## Correctness
Deterministic context-sufficiency check (missing_symbols ⊆ ∅). No live LLM execution —
declared substitute per K.6.8 (cost/availability constraint, honest limitation).

## Overhead (deterministic retrieval; see raw JSON per-cell timings)
- grep search: ~4–5 ms per task
- graph build (cold): dominated by full-repo AST index; cached warm hits for later cells
- slice: <1 ms after graph load
Local overhead was secondary per spec K.6.9 and not decisive.

---

# Final Architectural Conclusion

## Classification: **TARGETED_USE** (hybrid `Grep → CodeGraph`)

Evidence:
- AST/CodeGraph slice is a **strong context compressor for dependency-dense workflow
  symbols** (T3: −86.5% tokens, complete).
- AST slice is **unusable for caller/consumer discovery** (T2 incomplete: −86.8% tokens
  but required consumers missing — the graph resolves callees, not reverse callers).
- AST slice **backfires when the target's own body is large** (T1: −48% — bigger than grep).
- Grep is **complete everywhere** (all A1 cells complete) and cheap (ms).

Therefore the architecture that the data supports:

```text
Grep → candidate discovery (files/symbols that mention the change target)
   ↓
CodeGraph slice → context compression of the chosen symbols' dependency closure
   ↓
Agent
```

NOT `Grep → Agent` alone (throws away the −86.5% compression opportunity on workflow
symbols), and NOT `CodeGraph → Agent` alone (reverse-caller discovery unsupported → the
T2 class of tasks would silently lose required context).

**RETAIN aspects:** CodeGraph slicing retained for dependency-closure compression of
execution/workflow symbols.
**TARGETED_USE:** gate slicing on (a) target body size and (b) whether the change needs
callers (then include a grep pass).
**INCONCLUSIVE:** real-LLM agent correctness (token reduction does not prove LLM
behavior; a representative sample of LLM runs with both contexts is the missing
evidence — declared, not faked).
**REMOVE:** not supported — the T3 win is real and large.

## What remains unproven / INCONCLUSIVE
1. Real agent correctness under both strategies (needs LLM runs; declared substitute
   used).
2. 2-hop vs 1-hop equivalence in general (identical here — sample of 3).
3. Whether the reverse-caller gap is fixable within the graph (graph build is
   caller-aware for imports; the slice direction is the limitation).
4. Noise metric reliability at line level (symbol-level ground truth is primary, recorded
   as such).