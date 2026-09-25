# Phase 5.0 — Risk-Aware Scoped Evidence: Design (Step 1)

Status: **DESIGN COMPLETE — pending review** (no implementation in this step).
Baseline under design: Phase 4.3 — `400 passed, 1 skipped`, GATE WORK PASS,
GATE SHIP PASS, `origin/main` at `4bf1877`.
Method: every claim below comes from direct source inspection (file:line
referenced), sealed-evidence artifacts on disk, or read-only experiments
executed for this document. Nothing executable was modified.

> **The purpose of Step 1 is to identify the minimum valid evidence
> boundary before selecting a performance target.**

---

## 1. Executive Summary

Asha's COMPLETE evidence path is already dominated by one cost: the
package-wide `pytest` run inside `_collect` (measured 359–374 s per
dispatch, ≈99.4% of validation time; everything else — git identity,
scope, ruff, mypy, seal, verify, ledger, canonicalization — is
≈0.7 s total). Scoped evidence means: for executions whose independence
is *proven* (`PROVEN_DISJOINT` + coherent profile + certain scope),
validate only what the change can actually affect, while keeping the
Phase 3 contract (seal → verify → ledger → canonical → replay →
commitment) byte-identical in shape.

### Required Design Decisions (answers)

| Decision | Answer | One-line evidence |
|---|---|---|
| **A** — Can CodeGraph prove affected-test closure? | **ONLY UNDER CONDITIONS** | Reverse walk works mechanically (0.003 s; from `asha.codegraph` → 6/39 test modules reachable), but the real graph contains **190 UNRESOLVED nodes including core class names** (`unk:GovernedScheduler`, `unk:AuthoritativeEvidence`, `unk:CodeGraph`, …). Dependents that edge into `unk:*` are invisible to reverse attribution → provable false-negative path. Closure is provable only when reverse reachability is closed over a *complete* attribution (no unresolved/ambiguous identity for the changed surface). Otherwise `insufficient proof → COMPLETE` (never a guessed subset). |
| **B** — Can Mypy be safely scoped? | **ONLY UNDER CONDITIONS** | Read-only experiment: `mypy b/y.py` (consumer file only) **does** surface the type error inside imported `a/x.py` (`exit=1, checked 1 source file`) — default `follow-imports=normal` transitive checking works; `--follow-imports=skip` hides it (`exit=0`). But `mypy .` on this repo fails outright (`exit=2`, duplicate-module error), the gate therefore enumerates paths explicitly (69–70 files), and a file-argument run never touches modules unreachable from its targets. Scoped mypy establishes "targets + forward import closure type-clean under repo `mypy.ini`" — equivalent to the gate's claim only when targets ⊇ (changed ∪ reverse dependents) per Decision A conditions. |
| **C** — Can Phase 3 evidence represent SCOPED without schema change? | **YES** (contract level), with one additive worker-record proposal | `AuthoritativeEvidence` fields are mandatory in replay (`_expect_exact_keys`), but values may be *empty* (`normalized_facts` defaults to `()`); `canonicalize_evidence` always serializes every key (absence impossible by construction). Replay **does not read check outputs at all** (replay.py:32–36 — "the authoritative schema does not carry check outputs"), so deeper vs shallower validation is invisible to replay; commitment hashes exactly `canonicalize_evidence()` bytes → deterministic regardless. `EvidencePolicy.MINIMAL/COMPLETE` already exists (`resolve_evidence_policy`, evidence.py:179). Proposal only: an additive key on the *worker* record (e.g. `validation_mode`) — free-form dict, digest-covered, no `SCHEMA` bump needed; decision deferred to review (Open Question 5). |
| **D** — Exact `SCOPED → COMPLETE` forcing condition | Fail-closed list, §6 "Unknown" + §16.4 | Any of: scope `status != certain`; classification ≠ `PROVEN_DISJOINT`; scope level S3/S4; reverse closure touching UNRESOLVED/EXTERNAL/ambiguous identity; unsupported dynamic mechanism in changed or dependent code; conftest/pytest-config/consumer outside indexed set; any exception during scoped computation; differential-validation divergence observed (governance revert). |
| **E** — Evidence sufficient for `PROVEN_DISJOINT → SCOPED` | Required set, §6 | Certain scope capture (STRICT) + `PROVEN_DISJOINT` from the classifier over a non-empty envelope + coherent `GovernanceProfile` + scope level ∈ {S0,S1,S2} + observed ⊆ declared (already enforced) + *proven-complete* reverse closure (A-conditions) + affected-test set computed conservatively + the resulting check matrix recorded verbatim in `checks[]`. Nothing short of each item is accepted. |

---

## 2. Current Evidence Pipeline

Traced from runtime code (not docs). Dispatch path, Phase 4.3:

| # | Stage | Input | Output | Auth. fields affected | FS/Git deps | Subprocesses | Validation | Failure behavior | Class |
|---|---|---|---|---|---|---|---|---|---|
| 1 | MCP `asha_dispatch_task` (mcp_server.py:~1240) | JSON-RPC args | structured result | none (pre-execution) | reads sealed records for envelope (`_dispatch_context`) | none | type + unknown-field gates, `_safe_id` | structured error, nothing executed | operational |
| 2 | `classify_task` (classifier.py:91) | task + envelope | `TaskClassification` | none (decides *how* evidence is produced) | none | none | missing-signal fail-closed matrix | `_unknown(...)` → FULL | derived |
| 3 | `governance_profile` (classifier.py) | classification | `GovernanceProfile` | none | none | none | eligibility flags | malformed → not eligible | derived |
| 4 | `route` (router.py) | profile + `fast_path_enabled` | `RouteDecision` | none | none | none | fail-closed mode selection | never FAST | derived |
| 5 | `GovernedScheduler.run` (scheduler.py:754) | workers, flag, `classification_context` | report dict | none yet | worktrees under `<root>.worktrees/` | `git worktree add/list/remove`, `git rev-parse` (dispatcher) | cycle/scope/conflict gates in `_decide` (scheduler.py:~640) | BLOCKED/DEFERRED/FAILED states | operational |
| 6 | `default_execute` | worker `cmd`, worktree | `rc`, `tail` | `exit_status` later | worktree | **worker command** | none here | `FAILED worker_exit_<rc>`, no evidence | operational |
| 7 | `_collect` (scheduler.py:443–564) | `rc`, `path`, worker, `base` | evidence record + state | **all authoritative fields** | worktree git | see §3 | see §3 | `INVALID_EVIDENCE`/`FAILED`, no sealed record | authoritative producer |
| 8 | `check_runner.run` (check_runner.py:61) | resolved scope | `checks[]` | worker record `checks[]` only | repo root | `ruff`, `pytest`, `mypy` | fail-closed exit codes, 600 s timeout | entry `failed` → `_collect` returns `FAILED verification_failed:*` | checks-performed (gate) |
| 9 | `evidence.seal` (evidence.py:81) | payload dict | sealed dict + `evidence_sha256` | digest over whole worker record | none (pure) | none | required keys (`schema`,`stage`,`scope`,`commit`,`tree_hash`,`observed_at`,`checks`,`authorized_to_ship`) | `EvidenceError` → `INVALID_EVIDENCE` | authoritative |
| 10 | `_atomic_json` write (scheduler.py, temp+`os.replace`) | sealed dict | `<evidence_dir>/<wid>.json` | record at rest | FS | none | atomic replace | OSError → `INVALID_EVIDENCE` | authoritative |
| 11 | `verify_worker_evidence` (evidence.py:~97) | record path + worktree | pass/raise | re-binds digest **and** live tree | git (tree hash) | `git` rev-parse of worktree | digest re-check + `authorized_to_ship is False` + live-tree rebind | raise → `INVALID_EVIDENCE` ("digest alone is not proof") | authoritative |
| 12 | `ExecutionLedger.record ×5` (evidence.py:355) | milestones | in-memory events | none (ledger-only by construction, evidence.py:326–329) | none | none | defensive snapshot | n/a (memory) | derived (bridge) |
| 13 | `ledger.derive_authoritative` (evidence.py:361) | target tree, observed scope, verdict | `AuthoritativeEvidence` (facts default `()`) | identity key `sha256(base:worker:generation)`, trees, scope, verdict | none | none | `AuthoritativeEvidence.create` derives key internally | n/a | **authoritative** |
| 14 | `canonicalize_evidence` (evidence.py:268) | `AuthoritativeEvidence` | UTF-8 canonical bytes | canonical representation | none | none | sort_keys + compact + sorted sets/facts | n/a | authoritative |
| 15 | replay (`replay.verify_record/verify_bytes`, replay.py) | canonical bytes | `ReplayResult` | verifies identity/scope-coverage/verdict-class/wire form | hermetic (no git/fs/clock) | none | exact key sets, fact vocabulary, reason classes | divergences list; malformed → `ReplayParseError` | verification |
| 16 | commitment (`compute_record_commitment`, commitment.py:74) | previous digest + evidence | chain head | chain linkage | none | none | 32-byte hex checks | `ValueError/TypeError` → malformed chain | verification |

Authoritative evidence produced by the scheduler currently carries
`normalized_facts = ()` — validation results live **only** in the worker
record's `checks[]`; replay/commitment never see them (§10).

## 3. `_collect` Decomposition

Source: scheduler.py:443–564 (single `try` block; any of
`OrchestratorError | ScopeError | EvidenceError | OSError` →
`INVALID_EVIDENCE`, record `evidence: None`).

| Operation | Purpose | Input | Output | Authoritative? | Scopeable? | Failure behavior | Measured |
|---|---|---|---|---|---|---|---|
| gate `rc != 0` | worker failed | `rc` | `FAILED worker_exit_<rc>` | gates (no evidence) | no | return, no record | n/a |
| `git status --porcelain` (447) | detect dirty worktree | worktree | dirty list | indirect (forces commit) | **no** (identity) | non-empty → commit path; git fail → `INVALID_EVIDENCE` | ≈51–55 ms (spawn floor) |
| `_commit_all` (conditional, 453) | create committable identity | dirty worktree | commit | indirect (`commit`,`tree_hash`) | no | commit fail → fail-closed (comment 451–452) | ms-scale; only when dirty |
| `git rev-parse HEAD HEAD^{tree}` (464) | target identity | worktree | `target_commit`, `target_tree` | **yes** (`commit`,`tree_hash`,`target_tree_sha`) | no | → `INVALID_EVIDENCE` | 56–81 ms |
| `scope_resolver.changed_files` (467) | observed scope | `base`, worktree | sorted paths (diff `--no-renames` + untracked) | **yes** (`observed_scope`) | no (definition of SCOPED input) | `ScopeError` → `INVALID_EVIDENCE` | ≈98 ms |
| covered/violations (470–477) | declared-vs-observed gate | observed, declared | `INVALID_EVIDENCE scope_violation:*` | gates | no | return, no record | sub-ms |
| `scope_resolver.resolve` (478) | scope level S0–S4 + `status` + affected list | paths | `{scope,status,per_file,checks,...}` | **yes** (worker `scope`; status feeds policy) | input to SCOPED decision | `ScopeError` → `INVALID_EVIDENCE` | 0.2 ms (empty) / 78–90 ms (1 .py: git show + AST + surface diff) |
| `check_runner.run` (480) | validation matrix | resolved, root | `checks[]` | worker `checks[]` only | **yes** (see §6/§9) | entry failed → `FAILED verification_failed:*`; all-skipped → `INVALID_EVIDENCE no_verification_ran` | see below |
| ├ `ruff check <changed_py or .>` | lint targets | changed py | entry | checks-only | already target-scoped | fail-closed | 125–343 ms (9 runs) |
| ├ `pytest tests/ -q` (ALWAYS package-wide) | test verdict | — | entry | checks-only | **the** SCOPED candidate | fail-closed | **359,156–374,281 ms (9 runs, median 365,139)** |
| └ `mypy <targets>` or `mypy asha` | type verdict | changed py / fallback | entry | checks-only | SCOPED candidate (§9) | fail-closed; `[]` → `skipped` (never silent pass) | 375–2,844 ms (median 2,233) |
| `git diff base target` (491) | record diff | refs | `diff` text | worker `diff` | no | → `INVALID_EVIDENCE` | ≈52 ms |
| payload build (493–516) | worker record | all above | dict | record content | fields unchanged by SCOPED | n/a | <1 ms |
| `evidence.seal` (517) | digest | payload | `evidence_sha256` | **yes** | no | `EvidenceError` → `INVALID_EVIDENCE` | 0.10 ms |
| `_atomic_json` (519) | durable write | sealed | file | **yes** | no | OSError → `INVALID_EVIDENCE` | ms |
| `verify_worker_evidence` (522) | seal + live-tree re-bind | path, worktree | pass/raise | **yes** | no | → `INVALID_EVIDENCE` | <1 ms + one git call |
| `_classify_independence` (526) | MINIMAL/COMPLETE policy | `resolved`, graph | `IndependenceClassification` | policy record | drives SCOPED gate | gap → UNKNOWN → COMPLETE | µs |
| `ExecutionLedger.record ×5` (534–543) | milestones | — | events | none (ledger-only) | no | n/a | µs |
| `derive_authoritative` + `canonicalize_evidence` (544–560) | contract object + bytes | trees/scope/verdict | `authoritative[wid]` bytes | **yes** | no (shape identical) | n/a | 0.01–0.1 ms |

**Cost facts (from sealed dogfood evidence + read-only timings):**
`pytest` ≈ 99.4 % of validation wall; git/process-spawn floor ≈ 50 ms per
call on this host; cryptographic operations are sub-millisecond.

## 4. Evidence Dependency Map

Per check: `check → normalized fact → governance verdict → authoritative
field → replay requirement → commitment input`.

- **ruff** → no normalized fact (not in `AuthoritativeEvidence`) →
  gate only (failure blocks sealing) → worker `checks[]` → replay:
  *nothing* (replay never reads checks) → commitment: *nothing directly*.
  **If removed from a SCOPED run:** the claim *"lint passed for the
  changed files"* weakens — but ruff is already target-scoped in
  COMPLETE, so SCOPED keeps it unchanged → no authoritative claim lost.
- **pytest (package)** → same channel: worker `checks[]` only →
  **If removed:** the claim *"the repository's test suite passed for
  this execution"* weakens to *"the affected-test subset passed"*.
  This claim does **not** enter `AuthoritativeEvidence`, replay, or the
  commitment — it lives at the `checks-performed` level and gates
  sealing via `verification_failed:*`. Therefore a *proven-complete*
  affected subset preserves the strongest **sound** statement while
  making the weaker one explicit; an *incomplete* subset makes the
  record misleading → fail-closed to COMPLETE (§7).
- **mypy (dependency_graph)** → same channel →
  **If removed/narrowed:** *"type-checked targets + import closure"*
  instead of *"enumerated packages"*. Same replay/commitment
  independence; equivalence requires Decision B conditions.
- **git identity / scope / seal / verify** → direct authoritative
  fields (`commit`, `tree_hash`, `observed_scope`, digest) → replay
  verifies identity + scope coverage + verdict class; commitment hashes
  canonical bytes → **never scoping candidates.**
- **verdict `evidence_sealed`** → `GovernanceVerdict` → replay's PASS
  class → commitment input bytes → must stay bit-identical semantics
  in both modes.

Mandatory answer, restated: *removing package-wide pytest weakens the
"all tests ran" claim — that claim is real but lives in `checks[]`,
outside the committed contract; it may only be narrowed under a
**proven-complete** affected-test closure, never a plausible one.*

## 5. COMPLETE Evidence Contract

What the current COMPLETE path **establishes** (claims, not activity):

1. *Observed scope* — `observed_scope` = diff vs `base` with
   `--no-renames` + untracked, gated `observed ⊆ declared`
   (else no record exists at all).
2. *Target tree identity* — `commit` + `tree_hash`/`target_tree_sha`
   from `rev-parse`, re-bound to the live worktree at verification
   (`verify_worker_evidence`: digest alone is not proof).
3. *Conflict status* — upstream: `_decide` gates (scope_status, cycle,
   `ConflictManager.assess`) determine whether evidence collection was
   even reached; `capture_mode` STRICT iff `status == certain`.
4. *Validation status* — `checks[]` entries: exact command class,
   exit code, duration, output tail; `status` `certain|uncertain`;
   any failure ⇒ no sealed record.
5. *Normalized facts* — **none** today (`normalized_facts = ()`,
   evidence.py:365 default; scheduler passes none).
6. *Replay consumes* — identity key, scope coverage (`covered()` over
   reads|writes), fact vocabulary (unused while facts empty), verdict
   reason class (`evidence_sealed` → PASS), canonical wire form.
7. *Commitment consumes* — exactly `canonicalize_evidence(E)` bytes,
   chained `C_n = SHA256(C_{n-1} || bytes)`, genesis over
   `base_tree_sha` (commitment.py:40–86).

"Checks performed" = `checks[]`. "Claims established" = the seven items
above. Phase 5.0 must never conflate the two.

## 6. Proposed SCOPED Evidence Contract

Applies only when every **Required** item holds; otherwise COMPLETE.

### Required (always)
- certain scope capture (`status == certain` → STRICT);
- classifier `PROVEN_DISJOINT` over a non-empty, digest-verified
  envelope (absence of records/peers stays UNKNOWN → COMPLETE);
- coherent `GovernanceProfile` + router decision unaffected
  (SCOPED is a *validation-depth* axis, orthogonal to FAST/FULL routing);
- scope level ∈ {S0, S1, S2} (S3 contract drift / S4 toolchain → COMPLETE);
- observed ⊆ declared (existing gate, unchanged);
- proven-complete reverse closure (Decision A conditions) — otherwise
  SCOPED is forbidden;
- the worker record identical in shape: same required keys, same
  seal/verify/ledger/canonical path, `authorized_to_ship: false`;
- the *actual* scoped commands recorded verbatim in `checks[]`
  (no claim about tests that did not run).

### Optional (omittable without weakening a governance claim)
- running test modules provably outside the affected closure;
- whole-repo mypy enumeration beyond the closure target set;
- re-running ruff over unchanged files (already target-scoped today).

### Targeted (evaluated against the affected scope)
- **ruff**: unchanged (changed py files — already targeted);
- **mypy**: `{changed} ∪ reverse_dependent .py` under repo `mypy.ini`
  (Decision B conditions; default `follow-imports=normal`);
- **pytest**: the proven affected-test module set (§8), same
  `-q` semantics, same `tests/` discovery rules;
- scope resolution: already per-path.

### Complete (inherently repository-wide; never scoping candidates)
- git identity + observed scope + diff;
- conflict/scope gates, seal, atomic write, live-tree verification;
- ledger, `derive_authoritative`, canonicalization, replay, commitment;
- anything whose input set is *defined* as "everything" (e.g. S4
  toolchain config, S3 public-API drift).

### Unknown (any one forces SCOPED → COMPLETE — Decision D)
1. `status != certain` (ambiguity may only elevate);
2. classification ≠ `PROVEN_DISJOINT` or profile incoherent;
3. scope S3/S4;
4. reverse closure touches UNRESOLVED/EXTERNAL/ambiguous identity
   (incl. any `unk:*` that could alias the changed surface);
5. unsupported mechanism detected in changed or dependent code
   (dynamic-import markers, reflection-string references, plugin/config
   loading, subprocess-launched code) — §9 fallbacks;
6. changed file outside the indexed set (config, generated, binary,
   conftest, pytest configuration);
7. any exception during scoped computation;
8. differential validation divergence observed (governance revert to
   COMPLETE, §11);
9. empty check list or all-skipped result (existing
   `no_verification_ran` guard stays authoritative).

Determinism: every input above is a pure function of (repo state at
`base`, changed paths, sealed envelope, indexed sources) — no clock, no
environment, no randomness.

## 7. Reverse Closure Analysis

**Question:** *given changed file X, which tests may depend on X?*

Mechanically (read-only experiment, this repo, 69 modules / 1,691 nodes /
15,710 edges, build 0.96 s): edges are `GraphEdge(consumer, target, kind)`
(codegraph.py:183–189), so reverse BFS over `(target → sources)` is exact
graph arithmetic: from `mod/sym:asha.codegraph` → **123 sources, 6 test
modules** (`tests.test_codegraph`, `tests.test_context_slicer`,
`tests.test_mcp_apply`, `tests.test_mcp_phase43`, `tests.test_mcp_server`,
`tests.test_phase21_mcp_plan`) of 39 total test modules — 33 would be
excluded from a scoped run. Time: 0.003 s.

**Soundness verdict: INCOMPLETE today** — a reverse walk over this graph
is *not* a proof of the dependent set:

| Mechanism | Observed in this repo | Effect on reverse closure |
|---|---|---|
| unresolved names | **190 UNRESOLVED nodes**, including `unk:GovernedScheduler`, `unk:AuthoritativeEvidence`, `unk:CodeGraph`, `unk:ConflictManager`, `unk:GovernanceVerdict`, … | A dependent whose edge terminates at `unk:X` is **not** attributed to the file that actually defines `X` → provable false negative (unsound skip). Root cause not isolated in Step 1 (candidates: string forward-refs, `TYPE_CHECKING`-guarded imports (ast_indexer.py:589–595 detects them), un-indexed consumers) → Open Question 10. |
| external | 125 EXTERNAL nodes (stdlib/third-party) | forward boundary already stops expansion; reverse from repo files unaffected → but any changed file *becoming* external-resolved changes attribution |
| star imports | **0 present** (mechanism maps to `unk:star:*`, boundary UNRESOLVED) | currently no effect; fail-closed by design if introduced |
| aliases | handled via `alias_table` with explicit unknown→`unk:` fallback | conservative when unresolved |
| conditional imports | recorded with `conditional` flag (ast_indexer walk, 578–585) | conservative (edges kept) |
| dynamic import | markers `dynamic_import:*` recorded; repo census: `importlib` 40 hits, `getattr` 7, `__import__` 4 | targets untracked → dependents of dynamic targets unattributable |
| reflection/registry strings | `scope_resolver._dynamic_reference` exists precisely because of this class | unattributable → COMPLETE |
| external deps | as above | outside repo tests' concern, inside claim boundary |

**Rule adopted by this design:** `insufficient proof → COMPLETE`,
never `→ guessed test subset`. Reverse closure may be *used* only under
Decision A conditions: changed-surface attribution is complete — every
identity that could denote the changed code resolves (no candidate
`unk:*`, no ambiguous bridge), test/benchmark consumers are inside the
indexed set, and no unsupported mechanism appears in the changed or
reachable-dependent code.

## 8. Test Impact Model

Conservative model (to implement in Step 2, no code here):

1. **Index set**: all `tests/**/*.py`, `asha/**/*.py`,
   `benchmarks/**/*.py`, `.jspace/**.py` (the runtime's own files are
   consumers too), excluding VCS/cache dirs — same rules as
   `scope_resolver.PYTHON_SKIP_DIRS`.
2. **Graph**: `index_module` (repo-wide, BFS not required — full index
   for the impact model; measured 0.96 s for 69 modules) →
   `build_graph` → reverse BFS from changed nodes (module AND symbol
   granularity; conservative union).
3. **Direct imports**: module edges `mod:tests.x → sym/mod:asha.y` ✓.
4. **Transitive**: reverse BFS hop closure ✓ (0.003 s measured).
5. **Package/module boundaries**: `mod:` nodes give module-level
   granularity — impact set reported as *test modules* (over-approx of
   individual test functions; deliberately conservative).
6. **Test discovery rules**: pytest discovers `tests/test_*.py` under
   `tests/` (repo reality: 39 test modules); scoped run passes module
   paths explicitly (`pytest <list> -q`) — same runner, same flags.
7. **Dynamic imports** (`__import__`/`exec`/`eval`/`importlib` call
   markers): if present in changed or dependent code → **fallback
   COMPLETE**.
8. **Reflection** (`getattr(x, name)`, string registries): detectable
   heuristically — heuristic *use is forbidden for selection*;
   detection alone forces **COMPLETE**.
9. **Plugin loading** (`-p` plugins, entry points): config-driven →
   S4 scope anyway → COMPLETE.
10. **Generated code**: paths ignored/generated (outside index set) →
    **COMPLETE**.
11. **Subprocess-launched code** (tests spawning `python -m asha`
    built from string lists): not visible as import edges → detection
    = presence of subprocess/module-string launch patterns in dependent
    tests → **COMPLETE** (this repo's CLI tests do spawn subprocesses).
12. **Configuration-driven behavior** (`pyproject`, `conftest.py`,
    env vars): any change to `conftest.py`/pytest config → **COMPLETE**
    (fixtures alter *all* tests' semantics); env-dependent tests are
    covered by the S4/S3 gates that already force COMPLETE.
13. **Default**: any construct outside the model → **COMPLETE**.

Empty affected set (no dependent tests found) is *allowed only* when
closure was proven complete; the run then still must satisfy the
existing `no_verification_ran` guard — interplay deferred to Open
Question 3.

## 9. Mypy Scoping Analysis

Read-only experiments (repo + isolated temp fixture):

| Command | Result | Wall |
|---|---|---|
| `mypy asha/codegraph.py` (warm cache) | `Success … 1 source file` | 0.8 s |
| same, cold temp `--cache-dir` | `1 source file` | 2.6 s |
| `mypy asha` (cold) | `29 source files` | 3.0 s |
| `mypy .` (cold) | **`exit=2`, 1 error, “errors prevented further checking”** (duplicate module — why the gate enumerates paths) | 3.0 s |
| gate combo `mypy asha/ tests/ .jspace/control.py` (cold) | `69 source files`, exit 0 | 12.1 s |
| temp fixture: `mypy b/y.py` (importer of erroring `a/x.py`) | **reports `a/x.py:2` error, exit 1** | 1.8 s |
| same + `--follow-imports=skip` | `exit=0` (error hidden) | 1.6 s |
| temp fixture `mypy .` | reports error, 4 files checked | 0.8 s |

Findings:
- **import following**: default `follow-imports=normal` surfaces errors
  in transitively imported modules even when only one file is named;
  `--follow-imports=skip` changes the claim (must never be used).
- **package discovery / config**: `mypy.ini` (`mypy_path =
  .:benchmarks/live_eval`, `explicit_package_bases = True`, per-module
  `ignore_missing_imports`) is discovered from cwd for every argument
  form — configuration inheritance holds.
- **equivalence**: NOT equivalent in general. A file-argument run checks
  only targets + their forward import closure; modules unreachable from
  targets are never analyzed; the gate's enumerated claim is wider.
- **cache**: affects wall time only (0.8 s vs 2.6 s), not the claim
  (mypy keys its cache by options).
- **stubs/namespace/generated**: repo uses `explicit_package_bases`;
  no `.pyi` stubs present; generated files are outside the index set →
  those cases fall to Open Question 2/COMPLETE.

**Decision B**: scoped mypy = `mypy {changed ∪ reverse-dependent .py}`
with repo config, default follow mode — sound **only if** the target set
covers Decision A conditions (dependents included). Otherwise
`SCOPED Mypy evidence = insufficient → COMPLETE fallback`.
`mypy.ini` is never weakened (Forbidden by §1).

## 10. Replay / Canonical / Commitment Compatibility

Code inspection answers (§11 questions):

1. **Can fields be empty?** Yes — `normalized_facts` defaults to `()`
   today; empty `reads`/`writes` are legal (`frozenset()`).
2. **Can fields be omitted?** No — `replay._expect_exact_keys` rejects
   any missing/extra key at every level (`observed_scope`,
   `normalized_facts[i]`, `verdict`).
3. **Mandatory fields**: all of `AuthoritativeEvidence` (11 incl.
   derived `execution_identity_key`); all of `ObservedScope`,
   `NormalizedFact` (5), `GovernanceVerdict` (2).
4. **Does `canonical()` distinguish empty from absent?** Absent is
   impossible in canonical form: `canonicalize_evidence` always writes
   every key; empty renders as `[]` (evidence.py:292–309).
5. **Does replay depend on validation facts SCOPED might omit?** No —
   replay explicitly does not read check outputs (replay.py:32–36); it
   reads identity, scope coverage, fact vocabulary (empty today), and
   the verdict reason class. SCOPED keeps `evidence_sealed` → PASS.
6. **Commitment determinism**: unaffected — `Cn = SHA256(C_{n-1} ||
   canonicalize_evidence(En))`; canonical bytes are a pure function of
   the contract object, which SCOPED does not reshape. Measured:
   verify of a 5-record chain = 0.08–0.09 ms.
7. **Schema-version change required?** **Not at contract level.**
   `SCHEMA = 1`/`schema_version = 1` describe the artifact shape, which
   SCOPED preserves. Optional additive proposal: worker-record key
   `validation_mode: "COMPLETE" | "SCOPED"` (free-form dict, covered by
   the record digest) — no version bump; decision = Open Question 5.

## 11. Differential Validation Plan

Equivalence proof between COMPLETE and SCOPED at **semantic evidence
level** (never "both exited 0"):

- **Procedure**: for one change set, run the SAME worker twice from the
  same `base` in two isolated worktrees — once with validation matrix
  COMPLETE, once SCOPED — then compare:
  `base_commit`, `base_tree_sha`, `target_commit`, `tree_hash`,
  `observed_scope` (reads/writes/capture_mode), worker `scope` +
  `status`, `verdict.status` + `verdict.reason_code`, sealed digest
  **semantic projection**:
  `(base_tree_sha, target_tree_sha, sorted(writes), capture_mode,
  status, reason_code)` — excluding `worker_id`/`execution_identity_key`
  (differ by construction) and `checks[]` *command lines* (differ by
  design), while requiring `checks[]` *statuses* equal where both modes
  run the same check class, and no `failed` entry in either.
  Any divergence ⇒ SCOPED bug ⇒ fail-closed COMPLETE + report.
- **Corpus** (deterministic fixtures, one per case):
  1. disjoint change (SCOPED expected to differ only in check depth);
  2. shared change (SCOPED must be refused → both COMPLETE, outputs equal);
  3. transitive dependency change (affected set must include transitive
     test consumers);
  4. test dependency change (tests-only edit → S0);
  5. unresolved dependency (unk boundary → forced COMPLETE);
  6. dynamic import (marker → forced COMPLETE);
  7. ambiguous scope (`status = uncertain` → forced COMPLETE);
  8. malformed evidence (digest tamper → `INVALID_EVIDENCE` in both).
- **Also compare**: `verify_worker_evidence` pass, replay
  `verified=True`, commitment chain `VALID` for both modes' outputs.
- **Runtime cost note**: one differential pair ≈ 2 × package pytest
  (≈12 min at today's numbers) — Open Question 9 for CI placement.

## 12. Token Benchmark Plan

For `asha_get_surgical_context` — methodology only (no implementation
in Step 1; nothing measured as tokens here):

- **Corpus**: fixed list of (file, symbol) targets across the repo
  (including big/small, cross-module, unresolved-heavy cases).
- **Per target record**: `full_source_bytes`, `context_source_bytes`,
  chars, serialized JSON bytes of the payload, `reduction_ratio`
  (bytes), token counts under **`o200k_base`** and **`cl100k_base`**,
  token-reduction ratios, tokenizer name + version + vocab file hash.
- **Real tokenizer availability (measured)**: `tiktoken: False`,
  `tokenizers (HF): True`, `transformers: False`. The HF `tokenizers`
  library ships no o200k/cl100k vocabulary, and tiktoken would require
  a package installation (forbidden in Step 1; environment change).
  → Step 2 prerequisite: provision a pinned tokenizer in an isolated
  venv (not the gates' venv), record version, re-run; until then the
  project reports **source bytes only** (current public claim, README).
- **Analysis**: bytes-vs-tokens correlation; report that byte reduction
  is a *proxy* and must not be relabeled as token reduction unless
  measured.

## 13. Performance Model

`T_total = T_classification + T_routing + T_scope + T_validation +
T_evidence + T_canonicalization + T_verification`, measured on the
existing COMPLETE path:

| Term | Measured (this host, read-only) |
|---|---|
| `T_classification` | 0.046–0.261 ms (Phase 4.3 live telemetry) |
| `T_routing` | 0.008–0.033 ms (same) |
| `T_scope` | `rev-parse` 56–81 ms + `changed_files` ≈98 ms + `resolve` 78–90 ms (1 py) / 0.2 ms (none) + `git status` ≈51–55 ms + `git diff` ≈52 ms ⇒ ≈ **0.30–0.35 s** (≈50 ms process-spawn floor each) |
| `T_validation` | ruff 125–343 ms; mypy 375–2,844 ms; **pytest 359,156–374,281 ms** ⇒ **≈ 360–376 s**, pytest ≈ 99.4 % |
| `T_evidence` | seal 0.10 ms + atomic write ms + live-tree verify <1 ms ⇒ **< 5 ms** |
| `T_canonicalization` | 0.01–0.02 ms |
| `T_verification` | commitment chain (5 records) 0.08 ms; replay = pure hermetic JSON (not separately timed; same order — Open Question note) |

No latency target is set in Step 1.

## 14. Resilience & Recovery Plan

What Step 2 implementation must test (expected state / verdict /
runtime mode / cleanup):

**Process**
- *worker crash* → `FAILED worker_exit_<rc>`, no sealed record,
  worktree removed by dispatcher cleanup, envelope unchanged.
- *validation crash* (ruff/pytest/mypy spawn error or timeout 600 s)
  → entry `failed` → `FAILED verification_failed:*`, no record.
- *partial evidence generation* — atomic write guarantees no truncated
  `.json`; observed in Phase 4.3: a killed dispatch left **no** partial
  evidence file, but **did** leave a registered worktree → cleanup
  test must assert `git worktree list` clean after kill.

**Locks**
- orphan lock (`.jspace/lock` pid with dead process), stale lock
  (mtime beyond bound), interrupted execution, repeated retry — each
  must resolve to a deterministic state (release-or-fail-closed) with
  verdict `INVALID_EVIDENCE`/`BLOCKED`, never a silent double-run.

**Environment**
- secret env vars must never enter telemetry/evidence/MCP error
  payloads (Phase 4.3 allowlist rules + `cmd`/`prompt` exclusion stay
  binding for SCOPED outputs too).

**Repository**
- unexpected mutation mid-run → live-tree re-bind fails →
  `INVALID_EVIDENCE`;
- dirty tree → `require_clean_tree` (ship path) / observed-scope
  mismatch → `INVALID_EVIDENCE`;
- target-tree mismatch → `verify_worker_evidence` raise →
  `INVALID_EVIDENCE`;
- SCOPED divergence in differential harness → runtime mode reverts to
  COMPLETE (governance-level fallback), reported, never silent.

## 15. Implementation Plan for Step 2

(Ordered, each step gated, tests written first per repo convention;
NO step executes before this document is approved.)

1. **Diagnose the 190 UNRESOLVED nodes** (Open Question 10): either
   fix attribution deterministically (e.g. unique-name bridge rule
   `unk:X → sole repo definition of X`, ambiguous → COMPLETE) or accept
   the conservative fallback. Decision gate for Decision A usability.
2. **Affected-test module** (pure, stdlib): full-repo index → graph →
   reverse closure → test-module set or `INSUFFICIENT` sentinel; unit +
   property tests (including dynamic/star/reflection fixtures).
3. **Scoped check builder**: produces the `checks[]` command list for
   SCOPED (`ruff` unchanged, `mypy` targets, `pytest <modules>`), with
   `validation_mode` recording (Open Question 5 pending).
4. **`_collect` integration behind runtime config
   `ASHA_SCOPED_VALIDATION` (exact `"1"` only, env-only, default off —
   mirroring `ASHA_FAST_PATH_ENABLED`; never inputSchema-reachable)**
   — the ONLY production touchpoint; every forcing condition of
   Decision D wired fail-closed at the entry of the scoped branch.
5. **Differential harness** (`benchmarks/run_differential.py`) over the
   8-case corpus; divergence → non-zero exit + report.
6. **Dogfooding on Asha itself** (reversible, tree clean after —
   Phase 4.3 lessons: probe files ignored, no tracked mutations).
7. **Telemetry counters** (operational only): `scoped_checked`,
   `scoped_taken`, `scoped_fallback_complete`, `false_scoped = 0`
   invariant, plus timings already modeled in §13.
8. **Docs/README** update + final Phase 5.0 report with real numbers.

## 16. Acceptance Gates

1. `docs/phase-5.0-design.md` exists (this file). ✓
2. Runtime behavior unchanged — baseline must remain
   `400 passed, 1 skipped` (verified after this read-only phase; any
   change ⇒ STOP). ✓ (test run recorded in the Step 1 report)
3. No production implementation modified — `git status` shows only
   this document. ✓
4. `_collect` decomposed — §3 (18 rows + per-check timings). ✓
5. Evidence dependencies mapped — §4 (per-check mandatory question
   answered). ✓
6. Reverse closure limitations documented — §7 (measured census). ✓
7. Mypy scoping behavior documented — §9 (7 experiments). ✓
8. Replay/canonical/commitment compatibility analyzed — §10 (7
   answers). ✓
9. Differential validation specified — §11 (semantic projection +
   8-case corpus). ✓
10. Token benchmark methodology specified — §12 (incl. measured
    tokenizer unavailability). ✓
11. Resilience gates specified — §14. ✓
12. Step 2 plan concrete — §15 (8 ordered steps). ✓
13. Open questions listed — §17. ✓
14. Decisions A–E answered with evidence — §1. ✓

Gates for Step 2 entry: this document approved; `GATE WORK`/`GATE SHIP`
remain mandatory per commit; baseline must stay `400 passed, 1 skipped`.

## 17. Open Questions

1. Adopt a deterministic `unk:X → unique definition` bridge rule to
   make reverse attribution complete, or stay conservative (scoped
   effectively disabled while 190 UNRESOLVED nodes remain)?
2. Where may a pinned tokenizer (tiktoken, o200k_base/cl100k_base) be
   provisioned without touching the gates' environment?
3. Empty affected-test set under proven-complete closure: keep
   package-wide pytest (current `no_verification_ran` interplay) or
   allow a targeted substitute check?
4. Runtime config name/source for SCOPED — proposed
   `ASHA_SCOPED_VALIDATION=1` (env-only, default off): approved?
5. Additive worker-record key `validation_mode`: accept without
   `SCHEMA` bump, or require a versioned change?
6. S0 docs-only changes: keep package pytest or scoped-empty?
7. Are `benchmarks/*` modules in-scope for the test-impact index
   (they import `asha`)?
8. Does SCOPED apply only on the FAST path, or to any proven-disjoint
   execution regardless of routing mode? (Design assumes the latter —
   validation depth ≠ routing mode.)
9. Differential pair ≈ 2 × 6 min package pytest: run in CI per PR, or
   on demand / nightly?
10. Root cause of the 190 UNRESOLVED nodes (incl. core class names)
    not isolated in Step 1 — diagnose first (Step 2.1).
11. `replay.verify_record` wall time not separately measured (hermetic
    pure-JSON; expected sub-ms) — confirm during Step 2.

Open questions: **11**.

## 18. Explicit Non-Claims

- This document does **not** claim SCOPED evidence is safe. Nothing in
  it authorizes shipping, bypassing, or weakening the COMPLETE path.
- It does not claim CodeGraph currently proves affected-test closure
  (Decision A: only under conditions; today the conditions are unmet).
- It does not claim targeted mypy ≡ gate mypy (Decision B: only under
  conditions).
- It does not claim token reduction of any size — only source-byte
  measurements exist; tokenizers are unavailable in this environment
  (measured).
- It does not set a latency target; `T_total` numbers are observations
  of the CURRENT COMPLETE path.
- `UNKNOWN` remains ≠ `PROVEN_DISJOINT`; `UNKNOWN`/`SHARED` never enter
  FAST_PATH; telemetry remains operational-only; replay/commitment
  remain runtime-inactive (tests only).
- No evidence schema, canonicalization, routing, classifier, or
  `_collect` behavior was changed to produce this document.
