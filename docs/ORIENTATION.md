# Asha — Project Orientation

Status: describes **actual current behavior** at HEAD `4d71e5b` (K.6.1 frozen).
Not an architecture proposal.

## What Asha actually is

Asha is a **governed execution engine for agent-driven repository changes**.
It is not merely a multi-agent scheduler. The three pillars:

1. **Governed execution** — agent tasks run in isolated worktrees, routed
   through a DAG coordinator with explicit state machine
   (`DAGCoordinator` in `asha/governance/dag.py`, `run_workers_dag`),
   scope checking, and environment resolution.
2. **Scope / evidence / differential validation** — each worker's failures
   are compared against a baseline; only *new* failures block a change
   (see `FailureIdentity` below).
3. **Context reduction** — AST/CodeGraph-based slicing that gives an agent
   the smallest *sufficient* context for a target symbol (K.6/K.6.1).

Entrypoints (verified): `run_workers_dag` (DAG), `run_worker_in_worktree`
(CLI + MCP run), `GovernedScheduler` (legacy), MCP server
(`asha/mcp_server.py`: `asha_status`, `asha_plan_dag`, `asha_run_spec`,
`asha_get_surgical_context`, `asha_dispatch_task`).

## Why ordinary tests are insufficient for agent-driven changes

An agent's patch is not a test author's patch. The failure modes that
governance exists to catch:

- **Scope creep** — the agent modified files outside its declared scope.
  `scope_resolver.py` computes changed files against a base commit and
  `scoping_decision` (worker_execution.py:162) fails closed on violations.
- **Mock pollution** — tests that pass because a mock was inserted, or
  because the test suite itself was edited, not because the code is correct.
  Governance compares *failures*, not pass-counts, and treats a modified
  test as part of the change, not as evidence.
- **Stale `.pyc` / mtime collision** — a leftover `__pycache__`/`.pyc` from
  the baseline run can be reused by the *current* verification run, masking
  a real regression (K.5.3 defect, see cache isolation below).
- **Incorrect delta attribution** — failures must be attributed to *this*
  change. A failure that already existed at baseline is pre-existing, not a
  regression. `FailureIdentity` makes that attribution exact.

## Worktree isolation

Each worker executes in its own worktree (`asha/worktree.py`,
`WorktreeDispatcher`). Baseline and current verification run against
different trees; the worker's mutations are confined to the worktree, so a
failing worker cannot corrupt the main tree or another worker's files.

## Cache-Isolated Verification and `_strip_check_caches`

Invariant (audited in K.6, defect fixed in K.5.3):

> Every execution pipeline crossing
> Baseline Execution → Source Mutation → Current Verification
> must isolate/strip verification-affecting caches before mutation or
> before current verification.

`_strip_check_caches(path)` (worker_execution.py:205) removes
`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache` from the
worktree at any depth. Verified call sites: `_run_one` in
`DAGCoordinator` (after baseline, before worker execution) and
`run_worker_in_worktree`. Without it, untracked cache dirs surface in
`collect_worker_evidence`'s observed scope as violations, and stale `.pyc`
bytes can be reused by the current pytest run, producing a false clean
pass (the K.5.3 stale-pyc race).

## Fail-closed scope / environment behavior

What the code proves (no stronger claim):

- `scoping_decision` blocks execution when declared scope does not cover
  the changed paths (K.5 T5/T7 evidence).
- `EnvironmentResolver.resolve_env_for_file` (env_resolver.py:151) resolves
  the Python executable and environment for a file; resolution failure
  raises `EnvironmentResolutionError` — the worker does not silently fall
  back to an arbitrary interpreter. `<sys.executable>`-style hardcoding was
  eliminated in earlier phases.
- `verdict_for` returns `UNKNOWN` when the baseline is unavailable and the
  current run has failures — *unknown is never treated as safe*.

## FailureIdentity and differential failure analysis

`asha/governance/delta.py`:

- `FailureIdentity` — hashable identity of one verification failure
  (check name, location, code, message). `extract_failures` parses
  `check_runner` output (pytest nodeids, ruff/mypy diagnostics) into a
  `frozenset[FailureIdentity]`.
- `delta_failures(current, baseline)` — new failures introduced by the
  worker. `baseline is None` returns ALL current failures (unknown is
  never treated as safe).
- `verdict_for(current, baseline, has_failed_checks)` —
  `NO_FAILURES | PRE_EXISTING_ONLY | NEW_FAILURES | UNKNOWN`, fail-closed:
  unparseable-but-failed checks force `UNKNOWN` via `has_failed_checks`,
  never a clean `NO_FAILURES`.

## Major subsystems and verified relationships

| Subsystem | File | Responsibility |
|---|---|---|
| DAG coordinator | `asha/governance/dag.py` | `DAGCoordinator` FSM, `_run_one` dispatches workers, calls `baseline_checks`, `_strip_check_caches`, `collect_worker_evidence`; `run_workers_dag` entrypoint |
| Worker execution | `asha/governance/worker_execution.py` | `run_worker_in_worktree`, `baseline_checks`, `_run_checks`, `default_execute`, `collect_worker_evidence`, `persist_failure_evidence`, cache stripping |
| Delta / verdict | `asha/governance/delta.py` | FailureIdentity, extract/delta/verdict |
| Scope | `asha/scope_resolver.py` | changed-file computation vs base, scope normalization, fail-closed decision |
| Environment | `asha/governance/env_resolver.py` | per-file interpreter/env resolution, `EnvironmentResolutionError` |
| Runner | `asha/runner.py` | `dispatch_runner` → `CommandRunner` / `AntigravityRunner`, primary argv, verify-command splitting |
| CodeGraph | `asha/codegraph.py` | module/symbol index graph, `closure` forward/reverse, SCC |
| Slicer | `asha/context_slicer.py` | `slice_context` → target source + dependency stubs + completeness state |
| Code search | `asha/code_search.py` | tree-sitter outline/pattern/trace |
| MCP server | `asha/mcp_server.py` | JSON-RPC stdio tools (plan/run/slice/dispatch/status), telemetry |
| Graph cache | `asha/graph_cache.py` | persistent index+graph build/load |

Verified relationships (test names): `tests/test_delta_check.py`,
`tests/test_k53_stale_pyc_regression.py` (cache race),
`tests/test_k61_codegraph.py` (reverse/budget/completeness),
`tests/test_scoped_evidence.py`, `tests/test_codegraph.py`,
`tests/test_context_slicer.py`, `tests/governance/test_dag_coordinator_parity.py`.

## K.6 / K.6.1 context-engine evidence

Frozen benchmark artifacts: `benchmarks/k6/` (K.6, frozen at `778e2f9`) and
`benchmarks/k6_1/` (K.6.1, frozen at `4d71e5b`). Tokenizer: tiktoken
`cl100k_base` 0.14.0. All claims below are **task-specific observations**,
not universal superiority claims.

| Task | Grep tokens | CodeGraph fwd | CodeGraph rev |
|---|---|---|---|
| T1 collect_worker_evidence | 6968 | 10311 (larger) COMPLETE | 3205 INCOMPLETE (budget) |
| T2 FailureIdentity | 1413 | 186 COMPLETE (missed consumers) | 791 COMPLETE (consumers found) |
| T3 default_execute | 9271 | 1248 COMPLETE | 3065 INCOMPLETE |
| T6 DAGCoordinator.run | 109721 | 10588 COMPLETE | 1608 INCOMPLETE |

Observations:

- **CodeGraph is NOT universally better than Grep.** T1: forward slice was
  *larger* than Grep (−48%) because the target body is 170 lines and the
  emitted stub set was large. Large reductions (T3 86.5%, T6 90.4% — the
  latter only after the qualified-method-name fix) are **task-specific**,
  not a universal property.
- **Reverse traversal matters.** T2's forward slice was small (186 tokens)
  but *incomplete*: it could not see consumers (`extract_failures`,
  `verdict_for`). The K.6.1 reverse traversal (bounded, `max_nodes`)
  discovers them.
- **Bounded traversal is mandatory.** Reverse expansion can explode;
  `max_nodes` caps it and the result is reported `INCOMPLETE` rather than
  silently truncated.
- **Deterministic context-sufficiency ≠ agent success.** Every completeness
  measurement is a symbol-presence check against manually fixed ground
  truth. **Agent-in-the-loop validation has NOT been performed** (no LLM
  API key in the execution environment) — no real-agent correctness,
  cost, or success claim is made.

## Retrieval workflow: Grep → CodeGraph → slice_context

1. **Grep for candidate discovery** — find which files/symbols matter
   (`code_search`, plain grep). Cheap, complete, no graph needed.
2. **CodeGraph for structure** — `closure` walks dependency edges
   (forward) or caller edges (reverse `edges_to`) with a node budget.
3. **`slice_context` for the agent payload** — target source + compact
   signature stubs of reachable dependencies, plus a completeness state
   the agent must respect.

The K.6 conclusion (hybrid) stands: grep finds candidates, the graph
compresses, the slicer emits.

## Forward vs reverse traversal

- **Forward** (default): follows `edges_from` — *dependencies* of the
  target. Good for workflow functions (T3) and leaf data models
  (forward T2 is small and correct for the model itself).
- **Reverse** (`reverse=True`): follows `edges_to` → *consumers/callers*.
  Required when a leaf target's correctness depends on how it is used
  (T2's `FailureIdentity` needed `extract_failures`/`verdict_for`).
- Direction is explicit in the API; both honor `max_nodes` and cycle-safe
  BFS visiting.

## COMPLETE / INCOMPLETE / UNKNOWN semantics

From `ContextSlice.completeness` / `completeness_reasons`
(context_slicer.py:46), conservative by design:

- **COMPLETE** — the declared slice policy finished within `max_nodes`,
  no unresolved nodes remained, and at least one dependency resolved.
  It does **NOT** mean repository-wide semantic completeness; it means
  complete *under the slicer's declared policy, traversal bounds, and
  available graph*.
- **INCOMPLETE** — budget exhausted, unresolved nodes present, or zero
  dependencies resolved, each with a machine-readable reason. Requires
  supplementary investigation before acting.
- **UNKNOWN** — target symbol not found / not indexed (e.g. a bare method
  name instead of `Class.method`, or an unindexed module). **UNKNOWN is
  not safe and not COMPLETE**; it must remain unresolved.

## Known limitations

1. `test_env_resolver.py::test_env_no_src_no_pythonpath` fails on clean
   HEAD — pre-existing, unrelated to K.6/K.6.1, not yet fixed.
2. Agent-in-the-loop validation not performed (see above).
3. Methods require qualified symbol names (`FailureIdentity.from_dict`,
   `DAGCoordinator.run`); bare names do not resolve.
4. Large-body targets (T1-class) can produce slices larger than grep.
5. `max_nodes` is a single global cap, not per-depth.
6. Completeness is per-declared-policy, never repository-wide.
7. Stub serialization (docstrings + signature heads) still dominates for
   high-fan-out functions.
8. Slicer still emits more stubs than strictly necessary for tasks whose
   required context is a subset of the closure.