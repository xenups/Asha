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
asha --root <path> run --spec <spec.json> [--apply]   # scheduler CLI
asha-mcp                                              # stdio JSON-RPC MCP server
```

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

Planning never guesses: overlapping writes within one generation, unknown
read/write sets, or dependencies without import facts downgrade the
conflict matrix to `uncertain` instead of a false `safe`.

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
| Verified test baseline | **194 passed, 1 skipped** (CPython 3.11+, stdlib only). |

Fail-closed everywhere: a missing fact defers or blocks the run; it never
unlocks one. `push` is not `done`, and a commit is not ship authorization.
