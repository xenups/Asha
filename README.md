# Asha-Harness

**Asha (اَشَه)** — Ancient Persian concept of universal truth, deterministic
cosmic order, and non-destructive harmony, set against *Druj* (chaos, entropy,
structural corruption).

## 1. What Asha Is

Asha is a **zero-daemon governance and execution harness for coding agents**.
Its job is to constrain and verify repository changes. Specifically:

- It is **not** an autonomous coding agent.
- It does **not** replace Git, CI, pytest, mypy, Ruff, or project-specific
  tooling — it drives them.
- It answers exactly one question: **does this change have sufficient
  evidence to be authorized to ship?**

Core chain:

```text
repository state
    ↓
ORIENT (facts + provenance)
    ↓
agent reasoning
    ↓
change detection
    ↓
semantic scope
    ↓
applicable checks
    ↓
execution evidence
    ↓
cryptographic binding
    ↓
ship authorization
```

## 2. Core Design Principles

Explicit invariants:

1. **Fail closed.** Missing transport, dirty tree, missing receipts, failed
   checks, unreadable or tampered evidence → refusal (exit 1). Never a pass
   by default.
2. **Deterministic scope hierarchy.** `S4 > S3 > S2 > S1 > S0` and
   `final_scope = max(detected_scopes)`.
3. **Unknown impact must never downgrade scope.** Ambiguity elevates
   (`uncertain` → S3); incomplete analysis never produces S1/S2.
4. **Ship evidence must bind to the exact Git tree being verified**
   (`commit` = HEAD, `tree_hash` = `HEAD^{tree}`).
5. **Working tree must be clean for ship authorization**
   (`git status --porcelain` strictly empty).
6. **Evidence must be machine-verifiable** (canonical SHA-256 digest,
   re-computable by any third party).
7. **Atomic mutation is separate from verification.** `diff_engine.py`
   writes; checks verify; neither substitutes for the other.
8. **Git push is not equivalent to task completion**, and a passing test
   suite is not equivalent to valid evidence.
9. **Asha is a tool for agents, not a Git hook.** It does not block every
   push; it is invoked deliberately at the ship boundary.

## 3. Architecture

| Component | Responsibility |
| --- | --- |
| `.jspace/control.py` | State/ledger, gate orchestration, ship authorization |
| `.hermes/tools/scope_resolver.py` | Semantic scope classification S0-S4 |
| `.hermes/tools/project_map.py` | Live project orientation: facts + provenance synthesis |
| `.hermes/tools/memory.py` | Institutional memory adapter (Mem0) + context synthesis |
| `.hermes/tools/check_runner.py` | Isolated subprocess execution and result capture |
| `.hermes/tools/evidence.py` | Clean-tree validation, tree binding, canonical hashing, evidence verification |
| `.hermes/tools/code_search.py` | AST/structural perception and impact tracing |
| `.hermes/tools/diff_engine.py` | Exact atomic source mutation |
| `.hermes/tools/orchestrator/` | Governed worker scheduling package (facade in `__init__.py`): conflict-safe dispatch, git worktree isolation, tree-bound evidence, coalesced reconciliation, generation-gated dispatch; `types`/`conflict`/`worktree`/`scheduler` modules |
| `.hermes/tools/dep_index.py` | Phase-2 dependency fact extraction (stdlib `ast`): normalized facts, UNCERTAIN markers, content-hash cache |
| `.hermes/tools/graph_state.py` | Phase-2 immutable versioned GraphState: reverse-index affected region, cycle/uncertainty fail-closed reconciliation |
| `.hermes/tools/orchestrator/integrator.py` | Phase-4 atomic governed tree integration (`--apply`): sealed-evidence binding (commit tree == `target_tree_sha`), single-commit staging, unified-tree verification gate, all-or-nothing rollback with zero debris |

Supporting scripts (documented in §10 and Appendix E): `scripts/update.py`
(atomic self-update), `scripts/bootstrap.{sh,ps1}`,
`scripts/uninstall.{sh,ps1}`.

### Project orientation (`orient`)

`project_map.py` answers *what is this repository?* **before** an agent
changes anything — with facts and provenance only, never opinions.

```bash
python .hermes/tools/project_map.py --quick                 # json to stdout
python .hermes/tools/project_map.py --standard --format markdown
python .hermes/tools/project_map.py --deep
python .jspace/control.py --transport <ssh|local> orient [--mode quick|standard|deep] [--format json|markdown] [--no-cache]
```

`control.py orient` is a ledger-free, read-only wrapper (no
`control.json` is created or modified); the transport declaration is still
mandatory like every other command.

| Mode | Contents |
| --- | --- |
| `--quick` | Git state, stack, layout, tooling, configuration locations |
| `--standard` (default) | + entry-point candidates, recent-git hotspots, generated candidates, warnings |
| `--deep` (on demand, never default) | + schema/model candidates and public symbol inventory |

**Fact + provenance model.** Every reported fact is
`{value, source, confidence}` (layout-style facts use `path` instead of
`value`). `confidence` is exactly one of `direct` (config file says so),
`detected` (observed on filesystem / in git), `inferred` (derived) — and is
never fabricated. Entry points are **candidates**, detected by lightweight
AST patterns; file classification uses `generated_candidates`, never
`generated_files`; hotspots are raw git signals (revision counts, authors,
touch counts) with no risk language.

**Cache and invalidation.** Orientation may cache to
`.jspace/cache/orient.json` (git-ignored — a cache can never dirty the
repository). Cache identity is `tree_hash`, keyed per mode. A **dirty
working tree bypasses the cache entirely** (`cache_key: null`): stale
orientation is never served for uncommitted or untracked changes.

**Limitations.** Orientation is deterministic synthesis over config files,
the filesystem, and git history. It does not understand the project
semantically in a compiler-grade sense, and it proves nothing about runtime
behavior.

## Memory

Two different knowledge sources, never mixed:

```text
ORIENT = current repository ground truth (project_map.py, authoritative)
Mem0   = historical / institutional memory (.hermes/tools/memory.py,
         advisory only)
```

**Precedence (mandatory): current repository facts > stored memory.** A
retrieved memory may never override a conflicting fact from ORIENT; the
machine-readable context keeps them in separate namespaces:

```json
{
  "current_facts": { "...": "orient result, authoritative" },
  "memory": {
    "repository_facts": [], "stale_repository_facts": [],
    "workflow_preferences": [], "historical_lessons": [],
    "decision_records": []
  }
}
```

| Category | Tree sensitivity | Semantics |
| --- | --- | --- |
| `repository_fact` | tree-sensitive (bound to observed `tree_hash`) | was true at some point; revalidated against ORIENT — conflicts become `status: stale, conflict: true`, **never deleted** |
| `workflow_preference` | tree-independent | persists across tree changes; not artificially tree-bound |
| `historical_lesson` | persistent advisory | lessons from previous tasks |
| `decision_record` | persistent historical | explanatory, not authoritative |

Roles, stated once:

```text
Mem0   = candidate retrieval
Asha   = deterministic relevance selection (gate in .hermes/tools/memory.py)
ORIENT = current truth
```

The memory flow is a gate chain, because **retrieval does not imply
selection** — Mem0 finds candidates, Asha decides which candidates may
become advisory context:

```text
task → Mem0 candidate retrieval → relevance gate → current-fact conflict
     gate → deterministic dedup → bounded top-k (default K = 3) → agent
```

Relevance decisions are deterministic and explained per candidate
(`evaluate_candidate`): hard rejects `repo_mismatch`,
`stale_repository_fact`, `current_fact_override`; strong positives
`target_path_match` / `target_symbol_match`; medium
`multi_token_overlap`; generic-token overlap alone is never sufficient
(`generic_token_only`). Audit shape (`search --explain`):

```json
{ "memory_id": "...", "accepted": false, "reason": "generic_token_only",
  "matched_entities": []}
```

Only accepted, deduplicated, bounded records reach the agent; rejected
candidates stay in the audit (debugging/tests), never in the context.
The two-layer agent view (`context --format text`) renders
`CURRENT REPOSITORY FACTS` and `HISTORICAL MEMORY (ADVISORY)` as
separate sections so the layers cannot be confused. A stale
`repository_fact` is rejected from advisory context but a historical
lesson is never deleted because a repository fact went stale.

Report terminology keeps the two evaluation lenses distinct:
`retrieval_useful` = deterministic provenance rule (retrieved record
content == task historical_lesson, mechanical);
`judge_useful` = the blinded memory evaluator's useful class. They
legitimately differ and are never equated. After the relevance-gate
change no new live benchmark has been run — no behavioral improvement
is claimed until a future benchmark proves it.

Writes happen only from explicit structured `memory add` calls — no
automatic conversation capture; secret-like content is refused.

Mem0 is **optional and advisory**: if it is unavailable or broken, `add`,
`search` and `context` fail closed (`MEMORY UNAVAILABLE`, exit 1) while
ORIENT / SCOPE / GATE keep working unchanged. Offline profile: chroma
under `.jspace/cache/mem0` (git-ignored), mem0's mock embedder, `infer=False`
(zero LLM calls). Memory never guarantees correctness — it only supplies
historical context.

```bash
python .jspace/control.py --transport <t> memory add --category historical_lesson --content "..."
python .jspace/control.py --transport <t> memory search --task "manifest serialization" [--limit 3] [--explain]
python .jspace/control.py --transport <t> memory status          # availability + counts
python .jspace/control.py --transport <t> memory context [--format text]  # two-layer view + gated retrieval
```

## Empirical Evaluation

Why: the architecture above claims to help agent software-engineering
workflows. `benchmarks/` measures that claim instead of asserting it, and
records honestly when a metric cannot be measured.

### Conditions (identical task set, identical tool binaries)

| Condition | Available Asha context |
| --- | --- |
| `baseline` | none |
| `orient` | ORIENT facts |
| `orient_mem0` | ORIENT facts + Mem0 lessons |

The only intended difference is the available context; no model execution
belongs to this harness (agent-layer outcomes are not run here).

### Metrics

| Metric | Answers | Layer | Availability |
| --- | --- | --- | --- |
| `source_of_truth_error_rate` | wrong first hypothesis | agent | null + reason (agent layer not executed) |
| time-to-first-correct-hypothesis | orientation speed | agent | null + reason |
| `regressed_decision_rate` | repeated disproven mistakes | agent | null + reason; retrieval/precedence measured as tool-layer proxies |
| `final_correctness_rate` | primary outcome | agent | null + reason |
| `verification_time_ms`, checks executed | verification cost | tool | measured (real replays) |
| `scope_accuracy`, under/over/uncertain | scope quality | tool | measured (real replays) |
| orient exposure, Mem0 retrieval, precedence | context reachability | tool | measured (real replays) |

Unavailable metrics are `null` with an `unavailable_reasons` map -- never
zero-filled. Counts are reported as `X / N`, never bare percentages.

### Ground truth

Per task in `benchmarks/tasks.jsonl`: `ground_truth_source` (authoritative
implementation files), `ground_truth_tests`, `ground_truth_scope`
(maintainer annotation per this document's scope policy),
`ground_truth_outcome` (merged commit + gate status), plus
`known_failure_modes` and `historical_lesson` (Mem0 experiment seeds).
All fields derive from observable repository history; agent answers are
never used as ground truth.

### Limitations

* Small, single-repository sample (see recorded `task_count`); counts, no
  significance claims.
* Task-selection bias: only tasks with replayable ground truth qualify.
* Scope ground truth measures conformance to the documented policy, not
  the validity of the policy.
* Verification replay uses current tool binaries against each era's own
  config; duration values vary between runs (classifications do not).

### Reproduce

```bash
python benchmarks/run_eval.py --condition all
```

Refuses a dirty tree (canonical runs only); `--skip-verification` for a
fast classification-only pass. Outputs `benchmarks/results/<run_id>.json`
(one per condition) plus a comparison `<run_id>.md`. `results/` is
gitignored (wall-clock data, run-local). The agent-layer observation
record shapes for future scored runs are the metric schemas above.

## 4. Scope Model: S0-S4

| Scope | Meaning | Typical changes | Verification |
| --- | --- | --- | --- |
| S0 | Non-runtime | docs, comments, test-only changes with no runtime footprint | direct/project-appropriate tests |
| S1 | Runtime leaf | private/local runtime helper with **proven** zero downstream consumers | focused tests |
| S2 | Internal package logic | runtime implementation change inside one package without contract drift | Ruff + pytest + mypy as configured |
| S3 | Contract / external impact | public signatures, exported types, schemas, downstream consumers, unresolved semantic impact | broader regression + type checks |
| S4 | Repository/toolchain | `pyproject.toml`, Ruff/mypy config, CI/toolchain/lockfile changes | repository-wide/toolchain checks |

```text
S4 > S3 > S2 > S1 > S0
final_scope = max(detected_scopes)
```

Fail-closed behavior:

```text
uncertain impact
    ↓
S3 or explicit uncertain state
    ↓
never downgrade to S1/S2 merely because analysis is incomplete
```

Detection rules as implemented in `scope_resolver.py`:

- **S4 (static path rules):** `pyproject.toml`, `ruff.toml`, `mypy.ini`,
  `setup.cfg`, `tox.ini`, lockfiles (`poetry.lock`, `uv.lock`,
  `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `*.lock`), `.github/**`,
  `.circleci/**`, `.pre-commit-config.yaml`, `.python-version`.
- **S0 (static path rules):** `*.md`/`*.rst`, `tests/**`, `docs/**`,
  `examples/**`, `benchmarks/**`, license files. Python files whose AST is
  byte-identical after the edit (comment-only changes) are also S0.
- **S3 (semantic):** the public API surface changed — public function or
  method signatures, class bases/class-level annotations, `__all__`; added or
  removed public API (this is why module renames/deletions elevate to S3);
  unparseable sources; any `uncertain` condition below.
- **S1 (semantic):** only private (`_`-prefixed) definitions changed **and**
  every changed name has zero call-sites in a repository-wide AST scan
  (`trace_impact`) **and** no ambiguity exists.
- **S2 (semantic default):** everything else runtime: module-level edits,
  public function bodies with unchanged signatures, private helpers with
  callers, non-Python runtime files.
- **`uncertain`:** dynamic constructs in the changed file
  (`eval`, `exec`, `globals()`, `locals()`, `__import__`, `importlib`) or a
  changed private name referenced as a string literal (reflection-style
  lookup). Sets `status: "uncertain"` and forces scope to at least S3.

`trace_impact` (the `--trace` mode of `code_search.py` and the caller count
inside `scope_resolver.py`) is a **structural, AST-based approximation** —
import/call/inherit usage maps and `Name`/`Attribute` call counting. It is
**not** a compiler-grade whole-program semantic call graph (see §13).

## 5. Ship Gate

```bash
python .jspace/control.py --transport <ssh|local> [--root DIR] check --stage ship
```

Ordered flow as implemented:

```text
1.  Validate --transport (exit 1 before any state is written)
2.  Load + validate ledger (schema, questions dict, transport pin match)
3.  Validate read receipts, open-question rule, checkpoint/question digests
4.  Verify prior evidence digest if .jspace/evidence.json exists (tamper → refuse)
5.  Require clean Git working tree
6.  Resolve semantic scope over changed / affected files
7.  Determine mandatory checks for that scope
8.  Execute checks in isolated subprocesses
9.  Seal evidence bound to HEAD and HEAD^{tree}, atomic write, digest roundtrip
10. Authorize ship (exit 0) or emit authorized_to_ship=false + exit 1
```

Mandatory checks per scope (as implemented):

| Scope | checks |
| --- | --- |
| S0, S1 | `pytest` |
| S2, S3, S4 | `ruff`, `pytest`, `mypy` |

Hard equivalences:

```text
push   ≠ done
commit ≠ ship authorization
tests pass ≠ evidence is valid
```

## 6. Evidence Format

`.jspace/evidence.json` — exact current schema (git-ignored):

```json
{
  "schema": 1,
  "stage": "ship",
  "scope": "S0",
  "commit": "b8bde7a3ab791884a841f2982006b3c8daf5d743",
  "tree_hash": "ce67f834e6d2326ebb12c8970431b3e09a075587",
  "observed_at": "2026-09-22T16:15:32+00:00",
  "checks": [
    {
      "name": "pytest",
      "scope": "package",
      "status": "passed",
      "exit_code": 0,
      "duration_ms": 655,
      "output_tail": "1 passed in 0.02s\n"
    }
  ],
  "authorized_to_ship": true,
  "evidence_sha256": "3427b9f8da9bb5bc7703c4920523c66bec13fea5d88268b695bf4570ab3944ee"
}
```

Field meanings:

| Field | Meaning |
| --- | --- |
| `schema` | Evidence schema version (currently `1`) |
| `stage` | Gate stage, currently `ship` |
| `scope` | Resolved S0-S4 scope |
| `commit` | Git HEAD commit hash |
| `tree_hash` | `HEAD^{tree}` hash |
| `observed_at` | Observation timestamp (UTC ISO 8601, second precision) |
| `checks` | Executed verification results (see below) |
| `authorized_to_ship` | Final authorization result (boolean) |
| `evidence_sha256` | Canonical digest excluding the digest field itself |

Each entry of `checks` — exact emitted fields:

| Field | Values / meaning |
| --- | --- |
| `name` | `ruff` \| `pytest` \| `mypy` |
| `scope` | `changed_files` (ruff) \| `package` (pytest) \| `dependency_graph` (mypy) |
| `status` | `passed` \| `failed` \| `skipped` |
| `exit_code` | Integer subprocess exit code; `-1` means the process could not run (error/timeout) |
| `duration_ms` | Wall-clock duration of the subprocess |
| `output_tail` | Last 2000 characters of combined stdout + stderr |
| `note` | Present only when `status` is `skipped`: `"no applicable target"` (then `duration_ms`/`output_tail` are absent and `exit_code` is `0`) |

Check target selection (as implemented): `pytest` runs `tests/ -q`; `ruff`
runs on the changed Python files, else `.`; `mypy` runs on the changed Python
files, else `.hermes/tools/` if present, else it is `skipped`.

## 7. Evidence Integrity

Digest procedure:

```text
payload = evidence JSON without evidence_sha256
canonical JSON =
    sort_keys=true
    separators=(",", ":")
    ensure_ascii=false

evidence_sha256 = SHA-256(canonical JSON)
```

Tree binding:

```text
commit    = git rev-parse HEAD
tree_hash = git rev-parse HEAD^{tree}
```

Both matter: the digest proves the artifact was not edited after sealing;
the tree binding proves *which exact tree* the checks ran against. Together
they make evidence portable — any third party can recompute the digest and
re-derive the tree from the commit.

Tamper detection:

```text
authorized_to_ship: false
        ↓
manually changed to true
        ↓
digest mismatch
        ↓
ship refused (exit 1: "evidence_sha256 mismatch")
```

The prior artifact is verified **before** anything else in the ship gate, so
a tampered file can never be silently replaced by a fresh run.

## 8. Typical Agent Workflow

Two distinct loops.

**Tight development loop — smallest useful checks:**

```text
edit
→ focused test
→ changed-file lint
→ inspect diff
```

**Pre-ship loop — uses the resolved scope:**

```text
orient (only when the repository is unfamiliar)
→ scope resolution
→ applicable checks
→ full required regression
→ evidence generation
→ ship gate
```

Asha does **not** force full-project checks for every intermediate edit. The
scope model exists precisely so the tight loop stays tight; the pre-ship loop
is where evidence is produced.

## 9. Example Real-World Task

Contract drift found in this repository's own CI:

```text
test expects manifest["file_list"]
writer emits manifest["files"]
        ↓
inspect producer (bundle_writer.py emits "files")
        ↓
inspect repository history (git log -S: writer never had "file_list")
        ↓
prove canonical contract (a second test already asserts "files")
        ↓
atomic one-line test correction (diff_engine.py SEARCH/REPLACE)
        ↓
direct test (pytest tests/test_compose_export.py -q → passed)
        ↓
full regression (pytest tests/ -q → 104 total, green)
        ↓
ship evidence (control.py check --stage ship)
```

Note the decision: the **test** was aligned to the writer's contract, not
vice versa — one source of truth, no backward-compatibility shim invented.

## 10. CLI Reference

`control.py` is always invoked as
`python .jspace/control.py --transport <ssh|local> [--root DIR] <command>`.

| Command | Purpose | Success | Failure |
| --- | --- | --- | --- |
| `init --goal G --next N` | Create ledger | prints goal + transport, exit 0 | missing/invalid `--transport` → exit 1, **no ledger written** |
| `read FILE...` | Record read receipts (persisted) | prints content, exit 0 | missing file / digest drift → exit 1 |
| `pulse --event tool --label X` | Tool heartbeat into ledger | exit 0 | invalid event → usage error, exit 2 |
| `check --stage work` | Ledger consistency gate | `GATE WORK: PASS`, exit 0 | any invariant broken → `CONTROL ERROR: …`, exit 1 |
| `check --stage ship` | Full scoped evidence gate (§5) | `GATE SHIP: PASS`, exit 0 | tamper / dirty tree / failed checks → exit 1 |
| `checkpoint --claim C --evidence P` | Seal evidence receipt | `checkpoint N: …`, exit 0 | missing/empty evidence file → exit 1 |
| `question --open TEXT` / `--close N --evidence P` / `--reopen N` | Manage open questions (dict keyed by qid) | exit 0 | close without `--evidence` → exit 1 |
| `status [--json]` | Dump ledger state | exit 0 | corrupt ledger → exit 1 |

Standalone tools (no `--transport`; see Appendix A for flags):

| Command | Purpose | Success | Failure |
| --- | --- | --- | --- |
| `python .hermes/tools/scope_resolver.py [--root D] [--base REF] [--json]` | Resolve scope for current changes | prints `scope=… status=… files=N`, exit 0 | git failure → `SCOPE ERROR`, exit 1 |
| `python .hermes/tools/project_map.py [--quick\|--standard\|--deep] [--format json\|markdown] [--root D]` | Orientation facts + provenance | JSON or markdown, exit 0 | git failure → `ORIENT ERROR`, exit 1 |
| `python .jspace/control.py --transport <t> orient [--mode M] [--format F]` | Ledger-free orientation wrapper | orientation output, exit 0 | missing `--transport` → `TRANSPORT GATE`, exit 1 |
| `python .jspace/control.py --transport <t> memory add --category C --content "..." [--fact-key K --fact-value V]` | Structured memory write | record json, exit 0 | secret-like content / unavailable Mem0 → exit 1 |
| `python .jspace/control.py --transport <t> memory search --task "..." [--limit N]` | Bounded task retrieval | retrieved list, exit 0 | unavailable Mem0 → `MEMORY UNAVAILABLE`, exit 1 |
| `python .jspace/control.py --transport <t> memory status` | Backend availability + counts | status json, exit 0 | — (reports unavailability as data) |
| `python .jspace/control.py --transport <t> memory context [--task T]` | ORIENT vs memory synthesis (stale conflicts persisted) | context json, exit 0 | unavailable Mem0 / bad ORIENT json → exit 1 |
| `python .jspace/control.py --transport <t> orchestrator --spec F [--keep-worktrees]` | Governed worker scheduling (delegates; no ledger writes) | report json, exit 0 only when every worker is DONE | missing transport / bad spec / failed run → exit 1 |
| `python .hermes/tools/code_search.py --outline F` | AST outline (no bodies) | symbol table, exit 0 | missing file → exit 1 |
| `python .hermes/tools/code_search.py --trace SYM --dir D` | Structural impact map | usage entries, exit 0 | unknown symbol → empty result, exit 0 (structural approximation, see §13) |
| `python .hermes/tools/code_search.py --verify-env` | ABI pin check (exact versions, never auto-installs) | `ENV CHECK OK`, exit 0 | drift → exit 1 with fix hint |
| `python .hermes/tools/diff_engine.py --file F --patch P` | Atomic SEARCH/REPLACE | `OK: patched F`, exit 0 | missing/ambiguous SEARCH → `ValueError`, exit 1, target untouched |
| `python scripts/update.py [--dry-run]` (+ `update.sh` / `update.ps1`) | 5-step atomic self-update (Appendix E) | `Asha is already up to date.` / updated, exit 0 | dirty tree, divergence, red gate → exit 1 (rollback) |
| `bash scripts/bootstrap.sh` / `powershell -File scripts/bootstrap.ps1` | One-shot toolchain bootstrap (Appendix C) | all gates green, exit 0 | any step red → exit 1 (fail-closed) |
| `bash scripts/uninstall.sh` / `powershell -File scripts/uninstall.ps1` | Zero-bleed teardown (Appendix D) | exit 0 | residue found → nonzero |

`check_runner.py` and `evidence.py` are libraries — they have no CLI;
`control.py` calls them.

## 11. Repository Layout

```text
hermes-disciplined-harness/
├── .hermes/
│   ├── venv/                      # dev venv (git-ignored)
│   └── tools/
│       ├── project_map.py       # live project orientation (orient)
│       ├── memory.py            # Mem0 adapter + context synthesis
│       ├── scope_resolver.py      # S0-S4 scope classification
│       ├── check_runner.py        # isolated check execution
│       ├── evidence.py            # sealing, tree binding, verification
│       ├── code_search.py         # AST perception / trace
│       ├── orchestrator.py        # compat script entry (shim -> package)
│       ├── orchestrator/          # governed worker scheduling package (1+2)
│       │   ├── __init__.py        # public facade (re-exports + __all__)
│       │   ├── types.py           # data contracts: states, hook, error
│       │   ├── conflict.py        # dispatch safety: scope + R/W matrix
│       │   ├── worktree.py        # git worktree lifecycle + isolation
│       │   ├── scheduler.py       # scheduling loop, reconcile, CLI main
│       │   ├── integrator.py      # atomic --apply: evidence gate + rollback
│       │   └── __main__.py        # python -m orchestrator (dir-form: CPython
│       │                           # runpy bootstraps before user code)
│       ├── dep_index.py           # Phase 2: dependency facts (stdlib ast)
│       ├── graph_state.py         # Phase 2: immutable graph + reconciliation
│       └── diff_engine.py         # atomic SEARCH/REPLACE
├── .jspace/
│   ├── control.py                 # ledger + gates + ship authorization
│   ├── control.json               # runtime ledger (git-ignored)
│   ├── evidence.json              # ship evidence (git-ignored)
│   ├── dependencies.json          # self-update targets
│   └── cache/                     # git-ignored
├── SKILL.md                       # read-gate skill file (root)
├── modules/self-monitoring.md     # default gate module
├── skills/
│   ├── pre-ship-quality-gate/SKILL.md
│   └── asha-update/SKILL.md       # /asha update trigger
├── scripts/                       # bootstrap, uninstall, update (sh/ps1/py)
├── tests/                         # 155 regression tests (see §14)
├── benchmarks/                    # measured benchmark runner + results
├── ruff.toml                      # centralized lint exceptions
├── mypy.ini                       # mypy_path for cross-module imports
└── .gitignore
```

## 12. Failure / Exit Semantics

```text
exit 0 = requested gate completed successfully
exit 1 = gate refused / verification failed
exit 2 = argparse usage error (bad flags/choices; stock argparse behavior)
```

Refusal reasons are always printed to stderr with their exact cause
(`TRANSPORT GATE: …`, `CONTROL ERROR: …`, `SHIP GATE REFUSED: …`,
`GATE SHIP: FAIL -- checks failed: …`, `SCOPE ERROR: …`).

Unexpected internal failures must never be converted into false
authorization: any unhandled exception exits nonzero, and evidence is only
written by the deliberate sealing step — a crash before sealing leaves the
previous artifact (verified or refused) in place, never a new "pass".

## 13. What Asha Does NOT Guarantee

- AST impact tracing is **not** perfect whole-program semantic analysis.
- Dynamic imports, `getattr`, registries, and reflection can cause
  uncertainty; Asha responds by elevating scope, never by guessing.
- S1 requires **proven** zero downstream impact; uncertainty must not
  produce S1.
- Asha does not prove business correctness.
- Passing tests do not prove absence of all defects.
- Evidence proves **what was checked and on which tree** — not that the
  software is universally correct.
- Project orientation is deterministic fact synthesis (configs, filesystem,
  git history, lightweight AST patterns) — it does not semantically
  understand the project, and entry points / generated files it reports are
  candidates, not guarantees.

## 14. Status / Verification

Measured on the current working tree (Windows 11, CPython 3.11.16,
`.hermes/venv`):

| Gate | Result |
| --- | --- |
| `pytest tests/ -q` | **154 passed, 1 skipped** (skip = environment probe in `tests/test_code_search.py:116`) |
| `ruff check .hermes/tools/ tests/` | **All checks passed!** |
| `ruff check .` (full tree) | 19 known errors, **all inside the generated A/B playground `benchmarks/live_eval/asha_eval/`** (intentionally messy synthetic fixture; not shipped code) |
| `mypy .hermes/tools/` | **Success: no issues found in 19 source files** (root `mypy.ini` sets `mypy_path = .hermes/tools`; `mem0.*`/`run_eval`/`run_live` marked `ignore_missing_imports`) |
| `mypy .jspace/control.py` | Success: no issues found in 1 source file |
| `mypy tests/` | **Success: no issues found in 19 source files** |
| `code_search.py --self-test` | PASSED |
| `diff_engine.py --self-test` | PASSED |
| Ship gate contract | `GATE SHIP: PASS` → exit 0 only after clean-tree scope resolution, checks, sealing and evidence verification (§5) |

Push is performed only after the ship gate exits 0 (§12/§13); verify the
current remote with `git ls-remote origin main`.

---

## Appendix A — Tooling & Environment Catalog (Reproducibility Matrix)

### Python runtime (measured)

| Component | Version |
| --- | --- |
| CPython interpreter | 3.11.16 (Windows, x86-64) |
| venv location | `.hermes/venv/` (git-ignored) |
| lint / type / test | ruff 0.16.8 · mypy 2.3.1 · pytest 9.1.1 |
| vector memory stack | chromadb 1.5.9 · mem0ai 2.1.0 |

### Pinned ABI-critical parser matrix

| Package | Pinned version |
| --- | --- |
| `tree-sitter` | **0.21.3** |
| `tree-sitter-languages` | **1.10.2** |
| `ast-grep-py` | **0.45.3** |

**Why the pins matter — the 2-argument constructor ABI hazard:**
`tree-sitter` **0.24+** changed its core `Parser` C ABI: the bindings
switched to a 2-argument calling convention (language + options) and the
internal C struct layout changed. `tree-sitter-languages==1.10.2` was
compiled against the **0.21.x ABI** (`get_parser(lang)` → single-argument
`Parser(language)`). Mixing a pinned parser wheel with a newer core
(>=0.24) causes a segfault or
`TypeError: Parser.__init__() takes 1 positional argument but 2 were given`
at first parse. The three pins are verified together by
`code_search.py --verify-env` (exact-string comparison; mismatch → exit 1).
**Do not upgrade one without re-verifying all three.**

### AST tool flags (`code_search.py`)

| Flag | Output |
| --- | --- |
| `--outline <file>` | Symbol declarations only (classes, defs, decorators, line ranges, nesting) — no bodies |
| `--pattern <pat> --file <f>` | ast-grep AST matches (line/col + snippets) |
| `--trace <symbol> --dir <d>` | Import / call / inherit usage map, compact `{file, line, usage_type}` entries |
| `--verify-env` | Fail-closed ABI pin check (never auto-installs) |
| `--self-test` | Built-in end-to-end self-test |

### MCP server declarations (stdio only)

| Server | Package | Transport |
| --- | --- | --- |
| `sequential_thinking` | `npx -y @modelcontextprotocol/server-sequential-thinking` | `stdio` |
| `remote_linux` | `@modelcontextprotocol/server-ssh` | `stdio` |

Both run strictly over `stdio` — no TCP ports, no HTTP listeners, matching
the zero-daemon invariant (measured: 0 new listeners spawned, §Appendix B).

### Operating policy (target-agnostic)

1. Every `control.py` invocation declares `--transport <ssh|local>`;
   omission or unknown value → exit 1 with zero state written; a session's
   transport is ledger-pinned and mixing is refused.
2. `.jspace/control.json` is the single source of truth for goals,
   checkpoints (SHA-256 receipts), and open questions (a `dict` keyed by
   qid). Never hand-edit it; it is git-ignored — the policy ships, the
   session state does not.
3. All file edits go through `diff_engine.py` (exact, unique match →
   temp + verify → `os.replace`); no partial application, ever.
4. Before task closure: scoped ruff + pytest + mypy for the resolved scope,
   then `check --stage ship` must print `GATE SHIP: PASS`.

## Appendix B — Empirical Benchmarks

**Environment (Windows 11, git-bash/MSYS, x86-64):** CPython 3.11.16, ruff
0.16.8, mypy 2.3.1, pytest 9.1.1, tree-sitter 0.21.3, tree-sitter-languages
1.10.2, ast-grep-py 0.45.3. Subject: `.jspace/control.py` (767 lines /
3,087 tokens — a real shipped file). Rerun:
`.hermes/venv/python benchmarks/bench.py --json`.

| Benchmark | Metric | Measured value |
| --- | --- | --- |
| AST outline vs raw read | Raw source | 767 lines · 3,087 tokens |
| | AST outline | 39 lines · 117 tokens |
| | **Token reduction** | **96.21 %** |
| | Parse latency (median, n=5) | **156.79 ms** |
| Atomic diff safety | Valid patch apply (median, n=5) | **109.96 ms** |
| | Rollback via inverse hunk (median, n=5) | **109.27 ms** |
| | Colliding/mismatched patch | **exit 1** · 115.19 ms · **zero corruption** |
| Fail-closed transport gate | `init` without `--transport` | **exit 1**, no ledger written |
| | `init --transport local` | **exit 0**, ledger pinned |
| | Mixed transport | **exit 1** (`Transport mismatch`) |
| Zero-daemon verification | Listening TCP ports before → after | 38 → 38 (**0 new listeners**) |

### Token Efficiency Matrix (empirical reductions)

| Scenario | Raw input (tokens) | Asha pipeline (tokens) | Reduction |
| --- | --- | --- | --- |
| File exploration (`control.py` outline) | 3,087 | 117 | **96.21 %** |
| Ambiguous method patching (Trial A) | 4,722 | 225 | **95.23 %** |
| Signature refactor + scope tracing (Trial B) | 4,738 | 65 | **98.62 %** |
| Regression detection (Trial C) | 2,371 | 131 | **94.47 %** |

Token footprint counts everything the pipeline reads from or writes to
stdin/stdout — no hidden LLM traffic.

### Wall-Clock Trade-Off (measured, honest)

Live A/B benchmark (`benchmarks/live_eval/AB.py`, cold-run reproducible):

| Trial | Vanilla | Asha | Overhead |
| --- | --- | --- | --- |
| A — ambiguous patch | 0.91 ms | 277.43 ms | ~300× |
| B — signature + scope | 8.92 ms | 5,977.42 ms | **~670×** |
| C — regressive feature | 9.95 ms | 1,181.32 ms | ~120× |

| Layer | What it costs | Who pays it |
| --- | --- | --- |
| **Local wall-clock** | Cold venv spawn + tree-sitter parse + multi-stage verification: **~100×–670× more local compute** (ms → up to ~6 s/trial) | The harness toolchain, on the machine |
| **End-to-end turnaround** | Model inference + network round-trips scale with context volume; **>95 %** context reduction shrinks tokens-per-step and payloads | The agent runtime, over the wire |

Net effect: cheap local silicon buys a drastically smaller context window —
total agent round-trip ends up equal or faster, while the destructive
regressions Vanilla ships in every trial are eliminated.

### Asha Impact (dimensions beyond tokens)

| Dimension | Before (raw) | After (Asha) |
| --- | --- | --- |
| Atomic integrity | Whole-file overwrite: one bad write corrupts the target | SEARCH/REPLACE → temp verify → `os.replace`; instant rollback (109.27 ms) |
| Cross-service awareness | Blind edits; downstream callers break silently | `--trace` usage maps catch interface breakage before it lands |
| Transport discipline | Implicit; SSH/local silently mixed | Mandatory `--transport`, ledger-pinned, omission exits 1 pre-write |
| Gate discipline | Warnings tolerated | Scope-matched checks must all pass before evidence is sealed |

## Appendix C — Cross-Platform Bootstrap

Requires only Python >= 3.10 with `venv` and a shell.

```bash
bash scripts/bootstrap.sh                                       # Linux / macOS
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1   # Windows
```

The bootstrap builds the venv, installs the pinned ABI matrix
(`tree-sitter==0.21.3`, `tree-sitter-languages==1.10.2`,
`ast-grep-py==0.45.3`, `ruff`, `mypy`, `pytest`, `chromadb`, `mem0ai`),
asserts `code_search.py --verify-env`, runs both tool self-tests, registers
the two MCP servers over `stdio` (skip with `ASHA_SKIP_MCP=1` / `-SkipMcp`),
and ends with ruff + mypy + pytest — fail-closed on every step. Idempotent.

Manual equivalent (Linux/macOS):

```bash
git clone <your-repo-url> hermes-disciplined-harness
cd hermes-disciplined-harness
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install \
  "tree-sitter==0.21.3" \
  "tree-sitter-languages==1.10.2" \
  "ast-grep-py==0.45.3" \
  "ruff" "mypy" "pytest" "chromadb" "mem0ai"
python .hermes/tools/code_search.py --verify-env   # must print ENV CHECK OK
python .hermes/tools/code_search.py --self-test
python .hermes/tools/diff_engine.py --self-test
python -m pytest tests/ -q
```

Windows (PowerShell):

```powershell
git clone <your-repo-url> hermes-disciplined-harness
cd hermes-disciplined-harness
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install "tree-sitter==0.21.3" "tree-sitter-languages==1.10.2" "ast-grep-py==0.45.3" "ruff" "mypy" "pytest" "chromadb" "mem0ai"
python .hermes\tools\code_search.py --verify-env
python .hermes\tools\code_search.py --self-test
python .hermes\tools\diff_engine.py --self-test
python -m pytest tests\ -q
```

Windows (CMD):

```cmd
git clone <your-repo-url> hermes-disciplined-harness
cd hermes-disciplined-harness
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install "tree-sitter==0.21.3" "tree-sitter-languages==1.10.2" "ast-grep-py==0.45.3" "ruff" "mypy" "pytest" "chromadb" "mem0ai"
.venv\Scripts\python.exe .hermes\tools\code_search.py --verify-env
.venv\Scripts\python.exe .hermes\tools\diff_engine.py --self-test
.venv\Scripts\python.exe -m pytest tests -q
```

## Appendix D — Zero-Bleed Teardown

```bash
bash scripts/uninstall.sh                                      # Linux / macOS
powershell -ExecutionPolicy Bypass -File scripts/uninstall.ps1   # Windows
```

Removes, in order: the two MCP server registrations (`hermes mcp remove
sequential_thinking`, `hermes mcp remove remote_linux` — skipped if absent,
never touching shared servers), the isolated venv, `.jspace/cache/` and any
stale `.jspace/lock`; then asserts zero lingering harness processes and zero
new listening ports. Idempotent; exits 0 on success; never touches files
outside the repo bounds.

## Appendix E — Atomic Self-Update (`/asha update`)

Targets live in `.jspace/dependencies.json` (self: origin/main, pinned;
optional `submodules` with per-repo `remote`/`branch`/`test_command`).
Protocol — every boundary fail-closed:

1. **Clean tree guard** — `git status --porcelain` must be empty; dirty ⇒
   refuse (exit 1).
2. **Fetch & inspect** — no new commits ⇒ `Asha is already up to date.`, exit 0.
3. **Fast-forward only** — `git merge --ff-only`; divergence refused.
4. **Dependency & ABI audit** — `code_search.py --verify-env`,
   `ruff check .`, `pytest tests/ -q`.
5. **Rollback on gate failure** — any red gate ⇒
   `git reset --hard HEAD@{1}`, exit 1 with the exact reason.

Ask the agent "update asha" or "/asha update" (skill:
`skills/asha-update/`). Use `--dry-run` first (inspect only, no mutation).

## Appendix F — Agent Direct Self-Adoption Prompt

> Copy this block verbatim into the system prompt of any agent that will
> work in a repository governed by Asha-Harness.

```text
ASHA-HARNESS DIRECTIVE (binding)

1. DETECT: Before any work, determine whether Asha-Harness is active in the
   current repository — presence of .hermes/tools/code_search.py,
   .hermes/tools/diff_engine.py, .hermes/tools/scope_resolver.py,
   .jspace/control.py, and SKILL.md. If all five exist, proceed to (3).
2. BOOTSTRAP: If any are missing, trigger the bootstrap autonomously:
   bash scripts/bootstrap.sh            # Linux / macOS
   powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1   # Windows
   The bootstrap must pass `code_search.py --verify-env` before any edit.
3. BIND: All code edits MUST go through .hermes/tools/diff_engine.py
   (SEARCH/REPLACE, atomic). All structural exploration MUST go through
   .hermes/tools/code_search.py (--outline / --pattern / --trace). Raw
   in-place file mutation is forbidden.
4. REFUSE: Any change that violates the transport locked in
   .jspace/control.json. When no ledger exists yet, initialize one:
   python .jspace/control.py --transport <ssh|local> init --goal <G> --next <N>.
5. GATE: Before task closure: resolve scope, run its checks
   (ruff / pytest / mypy as scoped), then
   python .jspace/control.py --transport <declared> check --stage ship
   must print GATE SHIP: PASS. Any red gate means the task is NOT closed.
   push != done; commit != ship authorization.
```
