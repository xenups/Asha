# K.6.1 — Frozen Benchmark Specification

Source commit: `778e2f9` (K.6) with K.6.1 slicer changes (see IMPLEMENTATION.md).
Tokenizer: tiktoken `cl100k_base`, version 0.14.0.
Measurement boundary: bytes → tokens counted on the STRATEGY EMITTED CONTEXT
(target source + dependency stubs), identical to K.6.

## Strategies

- `Grep(A1)` — files under `asha/` containing the symbol (bare name for
  qualified `Class.method` targets), ±50-line windows around each hit.
- `CodeGraph-fwd` — forward dependency closure slice (pre-K.6.1 behavior).
- `CodeGraph-rev` — reverse caller/consumer closure, `max_nodes=50`.
- `CodeGraph-both` — forward slice + reverse slice concatenated.

## Ground truth

Per task, `required_symbols` were manually reviewed against the actual
source at `778e2f9` BEFORE running the benchmark. Completeness = every
required symbol present in the emitted context (word-boundary regex).

Completeness states (slice-level, from `ContextSlice.completeness`):
- `COMPLETE` — slice policy finished within budget, no unresolved nodes.
- `INCOMPLETE` — budget exhausted OR unresolved nodes OR zero deps.
- `UNKNOWN` — target symbol not indexed / not resolvable.

## Tasks

| id | target | module | required | grep baseline |
|---|---|---|---|---|
| T1 | collect_worker_evidence | asha.governance.worker_execution | collect_worker_evidence, baseline_checks, run_worker_in_worktree | 6968 |
| T2 | FailureIdentity | asha.governance.delta | FailureIdentity, extract_failures, verdict_for | 1413 |
| T3 | default_execute | asha.governance.worker_execution | default_execute, dispatch_runner, execute | 9271 |
| T4 | FailureIdentity.from_dict | asha.governance.delta | from_dict, FailureIdentity, to_dict | 1069 |
| T5 | run_worker_in_worktree | asha.governance.worker_execution | run_worker_in_worktree, baseline_checks, collect_worker_evidence, dispatch_runner | 7147 |
| T6 | DAGCoordinator.run | asha.governance.dag | run, DAGCoordinator, _run_one, run_workers_dag, _set | 109721 |

T1/T2/T3 reuse the frozen K.6 task definitions; T4/T5/T6 are new.

## Rules

1. Results are recorded as measured; no metric is dropped because the
   context is incomplete. Incomplete slices are reported separately.
2. Token reduction is relative to Grep(A1) tokens for the same task.
3. Determinism: same inputs → same output (single-threaded, no clock).
4. No LLM is involved in measuring; Phase 5 (agent-in-the-loop) is a
   separate, explicitly optional phase.