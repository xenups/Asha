# Asha — Governed Git-Native Multi-Agent Orchestrator & MCP Server

> **The Golden Law:** `PASS(A) + PASS(B) != PASS(A ∪ B)`

Asha is a zero-external-dependency, fail-closed system engine that isolates
AI coding agents into dedicated Git worktrees, checks AST dependency
conflicts before anything runs, and only merges work back through atomic
integration verified against a unified gate. Every stage — dispatch,
execution, evidence, ship — is refused unless provably safe: **unknown is
never treated as safe**.

- **stdlib only** — no LLM SDKs, no orchestration frameworks, no daemons
- **hermetic workers** — each worker executes in its own detached worktree against an immutable base commit; the main tree is never mutated in place
- **cryptographic evidence** — every completed worker seals `HEAD^{tree}`, observed file scope, and check results into tamper-evident `evidence.json`; integration re-verifies the seal before staging anything
- **one MCP server, seven tools** — usable from any MCP client (Antigravity, Hermes, Claude Desktop, Cursor, Windsurf)

## Quickstart

```bash
pip install -e .          # editable install; registers the console scripts
```

Registered CLI commands:

```text
asha [--root <path>] [--paths <file>...] [--json] [--no-execute] [--ui] [--ui-out <path>]
asha inspect <file> [--ui-out <path>]
                                              # governance CLI (schema v1)
asha status [PATH] [--root <path>] [--json]
                                              # read-only tip facts; a bare
                                              # positional PATH maps to root
asha --root <path> run --spec <spec.json> [--apply]   # scheduler CLI
asha-mcp                                              # stdio JSON-RPC MCP server
asha-mcp TOOL [k=v ...] [PATH]                        # one-shot tool call; bare
                                                      # PATH maps to root=PATH
.claude/commands/asha.md   →  /asha                   # thin passthrough
```

`asha` (the default command) is a thin adapter over the existing engine:
git state -> scope resolver -> classifier -> eligibility engine, then the
existing execution authority: one synthesized validation worker through
the orchestrator against a committed target (Spec 6.5.5 — only the
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
  `error: "REPOSITORY_CONFLICT"` — an operational condition, never a
  synthesized verdict. `--paths` accepts relative or absolute paths under
  the repository and normalizes them to the identical sorted form;
  anything outside the repository exits `2`.
* `/asha` thin wrapper: `.claude/commands/asha.md` is a pure passthrough
  (`asha $ARGUMENTS`); it inspects nothing and decides nothing.
* stdout: human result block (file states listed under `Changes`), or
  exactly one JSON document with `--json`
  (no progress logs; diagnostics go to stderr). ANSI color appears only
  on a TTY; `--no-color` (or `NO_COLOR`, or piped output) disables it.
* `--ui` / `--ui-out <path>`: after the run completes, also write a
  passive HTML inspection report (default
  `.jspace/reports/inspector.html`); the path goes to stderr, stdout
  contract unchanged. `asha inspect <file>` renders the same report
  from an existing artifact JSON with zero git/governance calls:
  recorded facts only (`"Not recorded"`/`"Unknown"` for absent fields,
  impact-set view when no edge data exists), single self-contained
  file, no script tags, no network references.
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

```yaml
command: asha-mcp            # or: python -m asha.mcp_server
```

The server speaks JSON-RPC over stdio on MCP protocol `2024-11-05`,
negotiated verbatim when the client requests it.

## MCP Tools Reference

| Tool | Behavior |
| --- | --- |
| `asha_status(root)` | Git repository cleanliness, current HEAD SHA, active worktree tracking, and lock detection — plus a deterministic, always non-empty `response` one-liner derived only from git facts (`tip <sha12>: clean\|dirty (N path(s)); worktrees X active, Y orphaned; control lock held\|free`), stable across every state: clean, dirty, detached HEAD, mid-rebase, orphaned worktrees. The `root` argument also accepts a bare positional path from the CLI (`asha status /path/to/repo`, `asha-mcp asha_status /path/to/repo`). |
| `asha_plan_dag(root, spec_content)` | Static AST dependency inspection, topological generations, write/read collision analysis, and safety classification (`safe` vs `uncertain`). Never executes anything. |
| `asha_run_spec(root, spec_content, apply)` | `apply=false`: simulation only — DAG rows as `SIMULATED_DONE`, zero subprocesses, zero side effects. `apply=true`: isolated execution in dedicated Git worktrees, cryptographic evidence collection (`HEAD^{tree}`), per-worker states and evidence paths. Atomic branch integration is a separate governed step (`asha --root … run --apply`) so execution evidence never doubles as ship authorization; failure rolls back with zero residue. |
| `asha_get_surgical_context(target_file, target_symbol, repo_path)` | Read-only Phase 4.1 pipeline: AST index → code graph → dependency closure → surgical slice for one symbol. Returns `target`, `context`, `dependencies`, `unresolved`, `full_source_bytes`, `context_source_bytes`, `reduction_ratio`. Every closure member is reported — UNRESOLVED boundaries are never silently dropped. `reduction_ratio` is SOURCE BYTES only, never a token claim. Phase 10 batch mode: optional `targets: [{target_file, target_symbol}, ...]` builds the AST index/graph ONCE per call and slices every requested symbol in a single pass; single-target calls stay byte-identical. |
| `asha_dispatch_task(id, declared_scope, reads, writes, deps, cmd, prompt, root)` | Dispatches ONE governed worker through the real pipeline: Phase 4.0 `classify_task` → `governance_profile` → Phase 4.2 `route` (fail-closed) → `GovernedScheduler` (worktrees, conflict gating, scope verification, sealed evidence). The classifier's context envelope comes from this repo's own sealed execution records (`.jspace/cache/orchestrator/*.json`) — no records means `UNKNOWN`, never a vacuous disjoint proof. Result carries `classification`, `reason_code`, `runtime_mode`, `routing`, `evidence`, `worktrees`, `timings`; `authorized_to_ship` stays `false`. Phase 10: `declared_scope`/`reads`/`writes` accept `"@diff"` / `"@diff:<base_ref>"` (reads auto-grow by the AST dependency closure of the changed files), and `execution_mode` selects `"worktree"` (default, existing behavior) or `"in_place_atomic"` — in-place runs `cmd` on the current branch tip, refuses a dirty/conflicted tree immediately (`TIP_DIRTY`/`TIP_CONFLICTED`), verifies observed changes stay strictly inside scope, and rolls back with `git reset --hard <pre_head>` + `git clean -fd` on ANY failure (`CMD_FAILED`, `SCOPE_VIOLATION`, timeout) with zero unsealed residue, sealing digest-bound evidence with `authorized_to_ship: false` on success. |
| `asha_verify_tip(root, base_ref, run_checks, declared_scope)` | Phase 9 in-place, fail-closed verification of the CURRENT working-tree tip, built for stacked-PR / multi-branch rebase workflows: no competing git worktree is ever spawned. A dirty, conflicted, mid-rebase, bad-base or out-of-scope tree is denied with a stable code (`TIP_DIRTY`, `TIP_CONFLICTED`, `TIP_DIFF_UNCLEAN`, `TIP_REBASE_IN_PROGRESS`, `TIP_BASE_UNKNOWN`, `TIP_SCOPE_MISSING`, `TIP_CHECKS_FAILED`) and no artifact is written. A clean tip binds to `snapshot_id_of(root)`, optionally runs the configured deterministic checks in-place, and seals digest-bound evidence (`evidence_sha256`) to `<state>/verify-tip/<head12>.evidence.json`; `authorized_to_ship` stays `false` — auditable pre-push verification evidence for `git push --force-with-lease`, never ship authority. `declared_scope` accepts the Phase 10 `"@diff"` / `"@diff:<base_ref>"` auto-scope sentinel (deterministic `git diff --name-only <base>...HEAD` expansion; protected `.github/`/`.jspace/` paths never unlocked implicitly; empty or unresolvable diff refused as `SCOPE_AUTO_EMPTY`). |
| `asha_verify_stack(branches, base_ref, root)` | Phase 10 ordered multi-branch (stack) verification, in place with zero worktrees: per branch it computes `head_sha`/`parent_sha`/`merge_base`, checks linear ancestry (`merge_base == parent_sha`), returns the exact recovery recipe `git rebase --onto <parent_sha> <merge_base> <B>` on drift, and re-verifies the sealed verify-tip evidence (`evidence_sha256`) for every branch head. `stack_healthy` is true only when EVERY branch is linear AND sealed. Refusal codes: `STACK_EMPTY`, `BRANCH_NOT_FOUND`, `BASE_REF_NOT_FOUND`; report codes: `STACK_ANCESTRY_DRIFT`, `STACK_EVIDENCE_MISSING`, `STACK_EVIDENCE_TAMPERED`. |

Planning never guesses: overlapping writes within one generation, unknown
read/write sets, or dependencies without import facts downgrade the
conflict matrix to `uncertain` instead of a false `safe`.

## Runtime & Telemetry Protocols

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

## Governance Phases Status (Roadmap)

Phases 1-4 (engine hardening, evidence sealing, and the Phase 4.0-4.3
classifier / surgical-context / router / fast-path + telemetry
capabilities) are described inline in the sections above. The roadmap
through the latest completed phase:

| Phase | Scope | Status |
| --- | --- | --- |
| Phase 5 | Cross-platform Windows/Linux test parity — 100% pass on both OS families | Complete |
| Phase 6 | Linux remote node integration (deployment, parity guide, live semantic reviewer) — see `docs/LINUX_SERVER_DEPLOY.md` | Complete |
| Phase 7 | Real-world anti-TOCTOU hardening & semantic governance verification | Complete |
| Phase 8 | Full stdio MCP orchestration (initial 5-tool server surface, JSON-RPC over stdin/stdout) | Complete |
| Phase 9 | Real-world telemetry hardening: positional CLI paths, deterministic `status` response, and in-place stack tip verification (`asha_verify_tip`) — driven by the Detailist stacked-PR production run (#222-#230) | Complete |
| Phase 10 | Stack-native execution: `asha_verify_stack` multi-branch lineage, `"@diff"` auto-derived scope, batch surgical context, and atomic `in_place_atomic` dispatch — 7-tool server surface, v0.3.0 | Complete |

Verified baseline: **919 passed, 0 failed** (CPython 3.11+, stdlib
only; green on both Ubuntu/Debian and Windows — skip counts are
platform-dependent and never mask a failure).

## Core Architecture & Governance Invariants

| Invariant | Guarantee |
| --- | --- |
| Physical worktree isolation | Each worker runs in its own detached worktree; zero shared working-directory mutations. |
| Process-tree containment | G3 lifecycle: one spawn primitive for everything; timeouts and cancellation call `kill_process_tree` (Windows `taskkill /T /F`, POSIX group kill) — no orphan processes. |
| Scope resolution | S0–S4 blast-radius classification of every change set; checks are selected by scope, never skipped. |
| Evidence sealing | Tamper-resistant `evidence.json`: commit, `HEAD^{tree}`, observed scope, check results; workers always seal `authorized_to_ship: false`. |
| Merge law | Worker green proves only its own tree; the integration gate re-runs the full matrix against the merged tree. |
| Verified test baseline | **919 passed, 0 failed** (CPython 3.11+, stdlib only; Ubuntu/Debian + Windows CI). |

Fail-closed everywhere: a missing fact defers or blocks the run; it never
unlocks one. `push` is not `done`, and a commit is not ship authorization.
