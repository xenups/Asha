# Asha — Project Orientation

Status: describes **actual behavior of v0.7.4** (`asha` 0.7.4, HEAD of `main` at
the release merge). Not an architecture proposal. Every path below exists in
this tree; the numbers were taken from the code, not from an older document.

The K6.1-era snapshot this file replaced is preserved verbatim at
[`docs/archive/ORIENTATION-K6.1.md`](archive/ORIENTATION-K6.1.md).

## What Asha is

A **governed execution engine for agent-driven repository changes**. Three
pillars:

1. **Governed execution** — workers run in isolated Git worktrees under a DAG
   coordinator (`asha/governance/dag.py`), with declared scope and an explicit
   state machine.
2. **Scope / evidence / differential validation** — the candidate's failures are
   compared against a baseline; only *new* failures block a change
   (`asha/governance/delta.py`, `FailureIdentity`).
3. **Context reduction** — AST/CodeGraph slicing that hands a worker the
   smallest sufficient context for a target symbol (`asha/ast_indexer.py`,
   `asha/codegraph.py`, `asha/context_slicer.py`).

## Entry points

| Entry point | Module | Shape |
| --- | --- | --- |
| `asha` (bare) / `asha PATH` | `asha/cli.py` (`_unified_cli`) | zero-config status + tip seal |
| `asha --verify` / `--stack` / `--context` / `--exec` | same | one-command modes, delegating to the MCP handlers |
| `asha [--root --paths --acceptance --json --no-execute --ui --watch --stepper]` | `asha/cli.py` (`main`) | governance over an explicit change set |
| `asha status` | `asha/cli.py` (`_status`) | read-only tip facts |
| `asha run --spec FILE [--apply]` | `asha/scheduler.py` | DAG scheduler over a run-spec |
| `asha dashboard` | `asha/cli.py` (`_dashboard_cmd`), `asha/observability/` | read-only loopback UI |
| `asha-mcp` / `python -m asha.mcp_server` | `asha/mcp_server.py` | 14 MCP tools, stdio JSON-RPC `2024-11-05` |

The CLI is a thin adapter: no policy of its own, every semantic field copied
from the engine.

## Module map (`asha/`)

**Governance core**

| Module | Role |
| --- | --- |
| `governance/dag.py` | `DAGCoordinator`, topological scheduling, per-worker state machine |
| `governance/worker_execution.py` | one worker: worktree, command/agent execution, evidence write |
| `governance/output_contract.py` | declared output contract read from the tree (writes / artifacts / no-change) |
| `governance/delta.py` | baseline vs. candidate failure identities and verdicts |
| `governance/evaluator.py` | worker verdict aggregation |
| `governance/env_resolver.py`, `verification_env.py` | interpreter and dependency resolution for the target repo |
| `governance/check_diagnosis.py` | environment-gap classification for a failing mandatory check |
| `governance/ship_gate.py` | canonical ship gate (extracted from the removed `.jspace/control.py`) |
| `governance/live_reviewer.py`, `semantic_review.py` | optional LLM-assisted review stages |
| `scoping.py`, `scope_resolver.py` | scope levels S0–S4 and the required check matrix |

**Execution and isolation**

`worktree.py` (creation/removal, source binding), `execution.py`,
`check_runner.py` (timeouts and durations), `runner.py`, `adapters/`
(`native`, `ci`, `agent`), `scheduler.py`, `worker_graph.py`, `router.py`,
`classifier.py`.

**Evidence and trust**

`evidence.py` (SHA-256 seal), `replay.py` (offline re-verification),
`contracts/` (`execution.py`, `validation.py`), `gate_states.py`,
`verification_profile.py` (operator-owned profile, repository binding, digest),
`commitment.py`, `audit_snapshot.py`.

**Context**

`ast_indexer.py`, `codegraph.py`, `graph_cache.py`, `graph_state.py`,
`context_slicer.py`, `code_search.py`, `dep_index.py`, `diff_engine.py`,
`project_map.py`.

**Review ladder**

`ladder/` (`orchestrator.py`, `target.py`, `artifacts.py`, `aggregation.py`,
`policy.py`, `instructions.py`, `models.py`) — the host LLM reviews, Asha
validates and decides.

**Surfaces**

`cli.py`, `mcp_server.py`, `presentation.py`, `report_format.py`, `ui.py`,
`telemetry.py`, `observability/` (events, store, server, static web assets),
`memory.py`, `metrics.py`, `metrics_cli.py`, `watcher.py`, `timeouts.py`.

## Invariants

1. **A seal is evidence, not a licence.** Worker evidence records
   `authorized_to_ship: false`; only `governance/ship_gate.py` decides shipping,
   and it re-runs the matrix against the merged tree.
2. **Skips are never passes.** A required check that could not run is recorded
   as skipped/`NOT_CONFIGURED` with provenance; an inventory of only
   `NOT_CONFIGURED` entries is `BLOCKED`, not `PASS`.
3. **Unknown is not empty.** An unparseable baseline is
   `NOT_COMPARED / UNKNOWN`, never `NO_FAILURES`.
4. **Silence is not success.** A worker that declared output and produced none
   fails `output_contract_not_satisfied`.
5. **Scope is closed.** Any observed path outside `declared_scope` is
   `INVALID_EVIDENCE`; unaccounted changes exit 2 with `SCOPE_INCOMPLETE`.
6. **Profiles never authorize.** `ASHA_VERIFICATION_PROFILE` (operator-owned,
   repository-bound, digest-snapshotted) changes *which checks are required*;
   it cannot grant a ship.
7. **Fail closed on trust.** An unreadable, unbound, digest-mismatched or
   unparsable profile aborts the run instead of degrading to a weaker matrix.
8. **Determinism.** `--json` emits exactly one document with stable field names
   and explicit `null`s; only `duration_ms` varies between identical runs.
9. **Observability is passive.** The dashboard records and renders; it cannot
   approve, reject, modify or seal anything.
10. **No hidden policy in the CLI.** Presentation lives in `cli.py`; every
    decision comes from the engine modules above.

## Scope levels and the default matrix

`scope_resolver.SCOPE_CHECKS`:

| Level | Checks |
| --- | --- |
| S0, S1 | `pytest` |
| S2, S3, S4 | `ruff`, `pytest`, `mypy` |

A verification profile replaces this matrix only where it applies, and only for
the services it names (`docs/verification-profiles.md`).

## State on disk

`asha/common/paths.py` resolves state in two zones:

- transient: the worktree's own temporary directories;
- persistent: `$ASHA_STATE_DIR`, else `$XDG_STATE_HOME/asha`, else
  `~/.local/state/asha` — holding `evidence/`, `journal/`, `orchestrator/`,
  `cache/` and the telemetry store.

`.jspace/` is no longer an execution path: the controller that used to live at
`.jspace/control.py` was deleted and its gate semantics moved into
`asha/governance/ship_gate.py`. The directory keeps only
`.jspace/dependencies.json`, which `scripts/update.py` reads for submodule
updates; the control lock itself lives in the state dir (`lock`), not there.

## Why ordinary tests are insufficient for agent-driven changes

- **Scope creep** — a patch that touches files the task never declared is
  refused, not diff-reviewed.
- **Mock pollution** — governance compares *failures*, so a test suite edited
  into passing is part of the change under review, not evidence for it.
- **Stale bytecode / mtime collisions** — a leftover `__pycache__` can mask a
  real regression; worktrees and cache isolation keep the two runs apart.
- **Pre-existing failures** — only new failure identities block, so a red
  baseline neither excuses nor blames the candidate.
- **Unconfigured tooling** — a repository-agnostic matrix would block a project
  that legitimately gates on other tools; a trusted profile is the way to say so
  without weakening the gate.

## Where to read next

- `docs/verification-profiles.md` — profile schema, trust rules, failure modes.
- `docs/WORKER-SKILL-POLICY.md` — mandatory Skill preload for workers.
- `docs/LADDER.md` — commit range to governed delivery decision.
- `docs/OBSERVABILITY.md` — telemetry schema, storage, dashboard API.
- `docs/ENVIRONMENT-DIAGNOSIS.md` — environment-gap classification.
- `docs/archive/INDEX.md` — historical design notes and frozen benchmarks.
