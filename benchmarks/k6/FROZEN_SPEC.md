# K.6 — Frozen Benchmark Specification: Cache-Isolation Audit + Context/Token Reduction

**Status: FROZEN. Do not modify after first valid execution.**
**Tokenizer:** tiktoken `cl100k_base` (frozen) — see manifest for exact version.

---

## Part 1 — Cache-Isolation Invariant Audit

Invariant under audit:

> Every execution pipeline crossing `Baseline Execution → Source Mutation → Current
> Verification` must isolate/strip verification-affecting caches before mutation or
> before current verification.

Audit scope: `asha/` production entrypoints — `run_workers_dag` (DAG), `run_worker_in_worktree`
(CLI + MCP run), `GovernedScheduler` (legacy). For each pipeline record entrypoint,
baseline, mutation, current verification, cache stripping + owner, evidence, verdict.
Classify A (protected) / B (missing protection) / C (not applicable). Only B requires fix.

Cache inventory (strong/weak staleness vectors): `__pycache__/*.pyc` (strong — bytecode
reuse, K.5.3-proven race), `.pytest_cache`, `.ruff_cache`, `.mypy_cache` (weak — perf).
Plus any in-process caches in check_runner / env_resolver (verified by grep: none).

Fix rule: minimal; single shared `_strip_check_caches` (owner `worker_execution`) invoked
by each orchestrator; no broad refactor; no duplicate policy.

---

## Part 2 — Context/Token Reduction Benchmark

### Research question

> Does AST/CodeGraph dependency slicing give an agent the context needed for a task with
> fewer tokens than text-based retrieval (Grep/full-file dump), without losing information
> required to complete the task?

No success threshold predefined. 50–70% is a hypothesis range, not a criterion.

### 2.1 Experimental unit

Per task fixed: repository commit, task specification, target outcome. Variable: retrieval
strategy only.

### 2.2 Tasks (frozen)

Target repository: **the Asha repo itself** (real multi-file dependency structures).

- **T1 — Signature Refactor:** `collect_worker_evidence(worker, path, rc, tail, task_id,
  evidence_dir, **kw)` — add a required keyword-only parameter and find every call site
  that must change (scheduler, DAG, worker_execution, tests).
- **T2 — Data Model Change:** `FailureIdentity` (`check`, `location`, `code`, `message`) —
  add a field `flaky: bool = False` and identify every consumer constructing/reading it
  (delta.py extract_failures, verdict_for, evidence payloads, tests).
- **T3 — Workflow/Async Change:** the dispatch path `DAGCoordinator.run()` → `_run_one` →
  `default_execute` → `dispatch_runner(worker).execute` — change the execution hook to add
  a pre-execution barrier and identify all callees/callers affected.

Target outcome per task: the set of files/symbols an agent must see to make the change
correctly (ground truth from code review of the actual change).

### 2.3 Strategies

- **A1 (Grep ±50 lines):** `rg <symbol>` → matched files → for each hit, ±50-line window.
- **A2 (Grep whole file):** `rg <symbol>` → matched files → full file text.
- **B1 (AST/CodeGraph 1-hop):** Asha `codegraph.build_graph` + `context_slicer.slice_context`
  on the target symbol; 1-hop callers/callees/inheritance/type refs.
- **B2 (AST/CodeGraph 2-hop):** same, 2-hop closure.

Primary comparison: **A1 vs B1** (both bounded context). A2 and B2 reported as secondary.

Relationship resolution honesty: any relationship the graph cannot reliably resolve is
recorded `UNKNOWN`. Never `UNKNOWN == SAFE`.

### 2.4 Metrics

Per task/strategy:

```text
required_symbols, provided_symbols, missing_symbols, extra_symbols
context_complete = required_symbols ⊆ provided_symbols
tokenizer: tiktoken cl100k_base
raw_chars, raw_lines, token_count
tokens_total, tokens_relevant, tokens_irrelevant
noise_ratio = irrelevant_tokens / total_tokens
token_reduction_pct = (A1_tokens − B_tokens) / A1_tokens × 100
grep_search_ms, ast_parse_ms, graph_build_ms, slice_ms, serialization_ms
```

Ground truth relevance at **symbol level** (file-level coarser; symbol-level primary).

### 2.5 Agent correctness

Deterministic context-sufficiency evaluation (no live LLM call — cost/availability
constraint declared up front): a judge (the harness's task oracle, built from the ground
truth) checks `required_symbols ⊆ provided_symbols` AND that the provided context contains
the concrete definitions needed (function signatures/bodies for the touched symbols).
This is explicitly a **substitute**, not a real agent run.

### 2.6 Repetition

Retrieval is deterministic → 1 retrieval run per strategy/task. Token counting
deterministic. No LLM randomness → no repetition beyond a 3× token-count stability check
(assert equal counts across repeats of the same inputs).

### 2.7 Decision framework

Classify: RETAIN / TARGETED_USE / INCONCLUSIVE / REDUCE_SCOPE / REMOVE, evidence-based.
Architectural question answered at the end: `Grep → Agent` vs `Grep → candidate →
CodeGraph → slice → Agent` (hybrid).

---

## Deliverables (benchmarks/k6/)

```text
FROZEN_SPEC.md   (this file)
CACHE_AUDIT.md
k6_harness.py    (retrieval strategies + measurement)
run_k6.py        (frozen tasks + orchestration)
results/k6_results.json
results/manifest.json
REPORT.md
EXACT_COMMANDS.md
```

## Stop conditions

Part 1 defect found → prove root cause → minimal fix → regression test → focused
verification → then Part 2. Part 2 not validly executable (no tokenizer / no task quality)
→ report INCONCLUSIVE with exactly what is missing; never fake results.