# Asha — Governed Git-Native Multi-Agent Orchestrator & MCP Server

**The Golden Law:** `PASS(A) + PASS(B) != PASS(A ∪ B)`

Asha is a zero-external-dependency, fail-closed system engine that isolates
AI coding agents into dedicated Git worktrees, checks AST dependency
conflicts before anything runs, and only merges work back through atomic
integration verified against a unified gate. Every stage — dispatch,
execution, evidence, ship — is refused unless provably safe: unknown is
never treated as safe.

- **stdlib only** — no LLM SDKs, no orchestration frameworks, no daemons
- **hermetic workers** — each worker executes in its own detached worktree
  against an immutable base commit; the main tree is never mutated in place
- **cryptographic evidence** — every completed worker seals `HEAD^{tree}`,
  observed file scope, and check results into tamper-evident
  `evidence.json`; integration re-verifies the seal before staging anything
- **one MCP server, three tools** — usable from any MCP client
  (Antigravity, Hermes, Claude Desktop, Cursor, Windsurf)

## Quickstart

```bash
pip install -e .          # editable install; registers the console scripts
```

Registered CLI commands:

```text
asha [--root <path>] [--paths <file>...] [--json] [--no-execute]
                                              # governance CLI (schema v1)
asha --root <path> run --spec <spec.json> [--apply]   # scheduler CLI
asha-mcp                                              # stdio JSON-RPC MCP server
.claude/commands/asha.md   →  /asha                    # thin passthrough
```

`asha` (the default command) is a thin adapter over the existing engine:
git state -> scope resolver -> classifier -> eligibility engine, then the
existing execution authority: one synthesized validation worker through
the orchestrator against a committed target (Spec 6.5.5 -- only the
scheduler may take the SCOPED runtime path, so an uncommitted target is
evaluated but never executed). It never re-decides anything: every
semantic field in its output is the engine's own.

CLI contract (schema_version 1):

* target discovery: `--root` defaults to the current directory and walks
  up to the git toplevel; without `--paths` the change set is one coherent
  snapshot (a single `git status --porcelain=v1 -z` read plus the scope
  resolver's own `base..worktree` membership source), reporting per-path
  states (`staged`, `unstaged`, `deleted`, `untracked`, `renamed`) inside
  `change_set.states`. Merge-conflict (unmerged) state exits `2` with
  `error: "REPOSITORY_CONFLICT"` -- an operational condition, never a
  synthesized verdict. `--paths` accepts relative or absolute paths under
  the repository and normalizes them to the identical sorted form;
  anything outside the repository exits `2`.
* `/asha` thin wrapper: `.claude/commands/asha.md` is a pure passthrough
  (`asha $ARGUMENTS`); it inspects nothing and decides nothing.
* stdout: human result block (file states listed under `Changes`), or
  exactly one JSON document with `--json`
  (no progress logs; diagnostics go to stderr). ANSI color appears only
  on a TTY; `--no-color` (or `NO_COLOR`, or piped output) disables it.
* JSON fields: `schema_version`, `repository`, `change_set`,
  `changed_files`, `decision`, `eligible`, `fallback_reason`,
  `execution_mode` (`targeted`|`canonical`), `validation_result`
  (`PASS`|`FAIL`), `duration_ms`, `evidence_id`, `evidence_verification`,
  `replay_verification`, `error`, `status` (`NO_CHANGES`). Missing data
  is explicit `null`; no absolute local paths; deterministic apart from
  `duration_ms`.
* exit codes: `0` validation completed (SCOPED or canonical COMPLETE) or
  `NO_CHANGES` / `--no-execute` evaluation success; `1` validation
  failed; `2` operational or engine error (including canonical
  execution refused on an uncommitted target and evidence verification
  failure); `130` interrupted (never reported as PASS).
* `COMPLETE` is not a failure: a canonical run that passes exits `0`.

`asha-mcp` and `python -m asha.mcp_server` are the same server. `--apply`
runs the governed integration path: single-commit staging, unified-tree
verification gate, all-or-nothing rollback with zero residue.

## Universal MCP Integration

### Google Antigravity

```json
{
  "mcpServers": {
    "asha": {
      "command": "python",
      "args": ["-m", "asha.mcp_server"],
      "cwd": "/absolute/path/to/Asha"
    }
  }
}
```

### Hermes Agent CLI

```bash
hermes mcp add asha --command python --args -m asha.mcp_server
hermes mcp test asha
```

### Claude Desktop / Cursor / Windsurf

Standard stdio connection using either entry point:

```text
command: asha-mcp            # or: python -m asha.mcp_server
```

The server speaks JSON-RPC over stdio on MCP protocol `2025-11-25`,
negotiated verbatim when the client requests it.

## MCP Tools Reference

| Tool | Behavior |
| --- | --- |
| `asha_status(root)` | Git repository cleanliness, current HEAD SHA, active worktree tracking, and lock detection. |
| `asha_plan_dag(root, spec_content)` | Static AST dependency inspection, topological generations, write/read collision analysis, and safety classification (`safe` vs `uncertain`). Never executes anything. |
| `asha_run_spec(root, spec_content, apply)` | `apply=false`: simulation only — DAG rows as `SIMULATED_DONE`, zero subprocesses, zero side effects. `apply=true`: isolated execution in dedicated Git worktrees, cryptographic evidence collection (`HEAD^{tree}`), per-worker states and evidence paths. Atomic branch integration is a separate governed step (`asha --root … run --apply`) so execution evidence never doubles as ship authorization; failure rolls back with zero residue. |
| `asha_get_surgical_context(target_file, target_symbol, repo_path)` | Read-only Phase 4.1 pipeline: AST index → code graph → dependency closure → surgical slice for one symbol. Returns `target`, `context`, `dependencies`, `unresolved`, `full_source_bytes`, `context_source_bytes`, `reduction_ratio`. Every closure member is reported — UNRESOLVED boundaries are never silently dropped. `reduction_ratio` is SOURCE BYTES only, never a token claim. |
| `asha_dispatch_task(id, declared_scope, reads, writes, deps, cmd, prompt, root)` | Dispatches ONE governed worker through the real pipeline: Phase 4.0 `classify_task` → `governance_profile` → Phase 4.2 `route` (fail-closed) → `GovernedScheduler` (worktrees, conflict gating, scope verification, sealed evidence). The classifier's context envelope comes from this repo's own sealed execution records (`.jspace/cache/orchestrator/*.json`) — no records means `UNKNOWN`, never a vacuous disjoint proof. Result carries `classification`, `reason_code`, `runtime_mode`, `routing`, `evidence`, `worktrees`, `timings`; `authorized_to_ship` stays `false`. |

Planning never guesses: overlapping writes within one generation, unknown
read/write sets, or dependencies without import facts downgrade the
conflict matrix to `uncertain` instead of a false `safe`.

### Fast Path runtime configuration (Phase 4.3)

`ASHA_FAST_PATH_ENABLED=1` is a **runtime configuration of the server
process** (exact value `"1"` enables; anything else or unset is the safe
`False` default). No MCP `inputSchema` carries this field, and no MCP
task, prompt, spec or any agent-produced payload can set or override it —
a client that sends it gets a structured `unknown field` error. Even
with the flag on, Fast Path still requires a coherent
`PROVEN_DISJOINT` classification; `PROVEN_SHARED`, `UNKNOWN`, malformed
or inconsistent profiles all stay on FULL GOVERNANCE.

### Telemetry vs evidence (Phase 4.3)

Every `tools/call` invocation appends one JSONL event to
`<repo>/.jspace/mcp_live_telemetry.jsonl` (`timestamp`, `tool`,
`request_id`, `duration_ms`, `status`, allowlisted `metadata` only —
never raw prompt, never raw `cmd`, never secrets or environment
values). Telemetry is **operational observability only**: not
authoritative evidence, not an audit proof, not tamper-proof, not a
commitment — and it is fail-safe: a telemetry write failure can never
change routing, authorization or execution semantics. Authoritative
evidence remains exclusively the sealed worker records produced by the
scheduler's `_collect` contract (`evidence.seal` + digest re-verify).

## Dependency Model: Code Graph → Worker DAG

Two graph layers, deliberately never collapsed into one:

| Layer | Nodes | Edges | Published by |
| --- | --- | --- | --- |
| Code Graph | files | AST dependency facts (`dep_index`) | `graph_state.reconcile()` |
| Worker DAG | workers | declared `deps` ∪ derived worker edges | `asha.worker_graph` projection → `TopologicalSorter` |

**Online reconciliation.** Every drained completion batch is coalesced
into ONE pass (overlapping completions are unioned first — never one
topology mutation per worker), analyzed incrementally from the sealed
evidence trees, and applied as a single atomic publication: GraphState
candidate, WorkerGraph projection, cycle validation — only then do both
graphs swap and the generation advance together. Any failure leaves the
previous GraphState *and* WorkerGraph standing by reference. Cached
facts and blob fingerprints ensure unaffected sources are never
reparsed.

**Derived worker dependencies.** For a file edge `src → tgt`, live
owners are resolved (workers whose `declared_scope` covers the path,
excluding workers that already completed): two different live owners
mean the consumer owner depends on the provider owner and must be
scheduled after it; same owner, missing owner, or no live owner yields
no worker edge. Declared `deps` survive every publication untouched.

**Generation-based invalidation.** A dispatch decision records the
generation it was computed against. After each publication the loop
builds a NEW sorter and replaces the readiness frontier wholesale:
stale READY entries are discarded, PENDING/DEFERRED workers re-decide
against dependency readiness + scope checks + ConflictManager, and
RUNNING workers continue — execution state is permanent, scheduling
state is not.

**UNKNOWN / fail-closed.** `UNKNOWN != EMPTY != SAFE`. Two live owners
of a path involved in a topology edge mark those workers
`owner_ambiguous` (blocked at dispatch, never guessed). A worker-level
cycle rejects the candidate WorkerGraph with the previous valid state
retained — a plain code-level import cycle alone never fails worker
scheduling. A batch where two workers produced the same file refuses to
choose (no last-write-wins) and fails closed.

**Add / delete / rename invalidation.** Reverse-dependency closure
reconsiders dependents of every changed file in both directions. A raw
unresolved import target is promoted to a real edge once the provider
file enters the known set — without reparsing unchanged sources.
Deletions pop their nodes; a rename is delete-old + add-new for
invalidation purposes, with no reliance on Git rename detection.

**Mixed / virtual integration analysis.** Concurrent worker outputs are
read as a deterministic Virtual Integration View: per pass each file
comes from the single producer whose sealed tree was just verified,
identified by a `virtual:` fingerprint in the run report. It is an
analysis identity only — never labeled a Git tree, never authorizing
integration or shipping; merge authority stays with the integration
gate.

## Task Specification Format (`spec.json`)

```json
{
  "workers": [
    {
      "id": "update_docs",
      "deps": [],
      "reads": ["README.md", "tests/"],
      "writes": ["README.md"],
      "declared_scope": ["README.md"],
      "cmd": ["python", "scripts/fix_docs.py"],
      "verify_command": ["python", "-m", "pytest", "tests/test_docs.py", "-q"],
      "timeout": 300
    },
    {
      "id": "refactor_scheduler",
      "deps": ["update_docs"],
      "reads": ["asha/"],
      "writes": ["asha/scheduler.py"],
      "declared_scope": ["asha/scheduler.py"],
      "agent": "antigravity",
      "task": "Split dispatch from evidence sealing; keep the FSM pure."
    }
  ]
}
```

- `deps` — topological ordering only; readiness is never safety.
- `reads` / `writes` — conflict-matrix inputs; `declared_scope` is checked
  against observed changes after execution (fail-closed on empty or
  unknown declarations).
- Command runner: `cmd` (argv list) plus an optional `verify_command`
  chain, which runs only after a zero primary exit.
- Agent runner: `"agent": "antigravity"` dispatches a headless agent
  (`--workspace <worktree> --non-interactive --task <prompt>`).

## Core Architecture & Governance Invariants

| Invariant | Guarantee |
| --- | --- |
| Physical worktree isolation | Each worker runs in its own detached worktree; zero shared working-directory mutations. |
| Process-tree containment | G3 lifecycle: one spawn primitive for everything; timeouts and cancellation call `kill_process_tree` (Windows `taskkill /T /F`, POSIX group kill) — no orphan processes. |
| Scope resolution | S0–S4 blast-radius classification of every change set; checks are selected by scope, never skipped. |
| Evidence sealing | Tamper-resistant `evidence.json`: commit, `HEAD^{tree}`, observed scope, check results; workers always seal `authorized_to_ship: false`. |
| Merge law | Worker green proves only its own tree; the integration gate re-runs the full matrix against the merged tree. |
| Verified test baseline | **229 passed, 1 skipped** (CPython 3.11+, stdlib only). |

Fail-closed everywhere: a missing fact defers or blocks the run; it never
unlocks one. `push` is not `done`, and a commit is not ship authorization.
