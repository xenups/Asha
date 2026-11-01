# Phase 12 — Monorepo verification integrity (F4)

Date: 2026-11-01
Base: `origin/main` `bdbcc42cd80dbf1b154b0c767bb7ab81a46e89c0`
Branch: `fix/monorepo-service-verification`

This addendum is additive. It does not edit, soften or delete any earlier
finding: the Phase 10 benchmark (0/3 authorized, 2.51× wall, 2.81× calls,
tokens/cost *not measured*, blind review Asha 3/3) and the Phase 11 audit
(`NOT READY — GOVERNANCE DEFECT`) stand exactly as published, including the
original F4 reproduction `rv-20261101T111103Z-52f94cf1`.

## 1. Why F4 existed

`asha/check_runner.py` built the pytest command from the **checkout root**:

```python
target = ['tests/'] if (root / 'tests').is_dir() else []
```

In a monorepo, the changed file lived under `pydetactl/`, and its suite lived
at `pydetactl/tests/`, but the runner only ever collected the repository's own
`tests/`. The suite that should have judged the change was never entered. The
gate then read a real, honest record — `"pytest" / "1 passed"` — about a suite
that had nothing to do with the change.

The defect was therefore **not** a missing test run; it was a verification
model with a single, unqualified scope. Evidence named `pytest` could not say
*which* suite passed, so the Ship Gate could not require the right one.

## 2. Reproduction on unpatched `origin/main`

Real stdio MCP server (`bdbcc42`), realistic review lifecycle (3 stages +
finalize), target `void-detailist/detailist` @ `899ab1c`, candidate adds one
lint-clean failing test at `pydetactl/tests/test_deliberate.py`:

```
state=FINALIZED decision=APPROVED gate_state=COMPLETED_CLEAN
authorized_to_ship=true head=5035ff7c01af533742d5ca4dd4bef07fb3c65fe1
changed=pydetactl/tests/test_deliberate.py, pydetactl/tests/test_plan.py
```

Same harness, same target, against this branch: `EXECUTION_FAILED`,
`authorized_to_ship=false`, record `pytest:pydetactl failed exit 1`.

Evidence: `evidence/pre_f4_repro.json` (pre-fix) and
`evidence/post_scenarios.json` (post-fix).

## 3. Which scope was missing, and how it is resolved now

The verification model now has two independent scopes:

| Scope | Owner | Evidence name | Working directory | Interpreter |
|---|---|---|---|---|
| repository suite | repository root `tests/` | `pytest` | checkout root | repository environment |
| service suite | `<service>/tests/` | `pytest:<service>` | `<service>` root | service environment |

Discovery is layout-first, marker-second (`_suite_root_for`): the service root
of a changed file is the nearest enclosing directory that carries a `tests/`
suite, falling back to the environment resolver's project-marker walk. A
service that is merely a directory with a suite is therefore still verified —
that is exactly the case that produced F4.

Rules that follow from it:

* the repository suite always runs, in its own environment, and is never
  merged into a service record;
* a service that has no suite produces a **proven skip** naming the service;
* a candidate that *deletes* the suite it was supposed to satisfy produces a
  **failed** record (`service suite <svc>/tests/ is missing from the candidate
  tree`), never a skip;
* unrelated services are not executed and not required;
* each suite keeps its own exit code, output tail, stdout/stderr digests and
  `service` label, so a root pass cannot be read as a service pass.

## 4. The inventory is a plan, not an output

`check_runner.verification_plan(root, resolved)` is the single declaration of
what this repository must produce for a change. `check_runner.run()` executes
that plan; `ship_gate._gate_evidence` **requires** it
(`check_runner.required_check_names`). Neither side trusts the other's output:

* planned but absent → `INVALID_EVIDENCE` (`required checks did not run:
  pytest:pydetactl`);
* present but unplanned → `INVALID_EVIDENCE` (unexpected check), so a suite
  from another checkout, service or task cannot buy authorization;
* present and failed → `EXECUTION_FAILED`.

The evidence schema, the journal and the artifact formats are unchanged: the
new information rides in the existing `name`/`service`/`exit_code` fields.
`semantic-review`, `ruff`, `mypy` and the F1/F2/F3/F5 semantics are untouched.

## 5. Environment and source binding

Service suites run through the existing `_service_env` / `verify_source_binding`
machinery: the resolver picks the service interpreter, `_env_cwd_env` sets the
working directory, `_exec_path_args` maps the suite path into a hermetic
layout, and a resolver that raises is recorded as `failed` with
`launched: false` (never an `import`-failure pass). A pytest invocation error
(exit 2/3/4) is a failure; only exit 1 is "the suite judged the change".

Acceptance used a bench environment whose interpreter sees both the Asha and
the target's dependencies (`bench/p12/ashap12`, `.pth` only, no product
change) so that a healthy service suite can actually execute — the target's
`pydetactl` suite needs `furl` from its own virtualenv.

## 6. What the live validation demonstrated

| Scenario | Result | pytest records |
|---|---|---|
| healthy root + healthy service suite | `COMPLETED_CLEAN`, authorized | `pytest passed 0`, `pytest:pydetactl passed 0` (37 passed) |
| root passes, service test fails | `EXECUTION_FAILED`, unauthorized | `pytest passed`, `pytest:pydetactl failed 1` |
| candidate deletes the required suite | `EXECUTION_FAILED`, unauthorized | `pytest:pydetactl failed` (missing) |
| service interpreter unusable | `EXECUTION_FAILED`, unauthorized | `pytest:pydetactl failed 127` |
| service collection error | `EXECUTION_FAILED`, unauthorized | `pytest:pydetactl failed 2` |
| untracked user file | `REFUSED`, unauthorized | refused before checks (dirty tree) |

"Evidence from another checkout" is proven at the contract level
(`tests/test_monorepo_service_verification.py::test_g_evidence_cannot_be_reused_from_another_checkout`):
the live MCP surface has no supported way to inject a foreign evidence
document without editing the journal, which is out of scope.

## 7. How the original false approval is prevented

The Phase 11 approval existed because `pytest` was unqualified and the plan
contained only that name. On this branch the same candidate produces two
records and the gate requires both:

```
pytest             passed 0   .  1 passed in 0.01s
pytest:pydetactl   failed 1   FAILED pydetactl/tests/test_deliberate.py::test_deliberate_failure
```

`gate_state=EXECUTION_FAILED`, `authorized_to_ship=false`. Removing the service
record instead of failing it yields `INVALID_EVIDENCE` — there is no shape of a
root-only run that authorizes a change whose service suite exists.

## 8. Scope, tests and limitations

Changed: `asha/check_runner.py` (suite discovery, per-suite naming, plan),
`asha/governance/ship_gate.py` (inventory = plan), plus
`tests/test_monorepo_service_verification.py` (12 regression tests covering
reproduction, healthy path, missing/duplicate/foreign evidence, unavailable
environment, collection error, unrelated service, suite deletion, root-suite
independence and the root-only layout).

Not changed: F2 diagnostics, F3 (target repository, PR #297), journal tamper
resistance, dirty-worktree protection, `.gitignore`, benchmark results.

Known limitations:

* suite discovery is "nearest `tests/` directory"; a service that keeps tests
  somewhere else (e.g. `spec/`) is not discovered and is not required;
* one pytest run per suite is a separate process: a monorepo change now pays
  one extra suite invocation per affected service;
* the repository suite runs on its own even when the change is service-local,
  so a root-suite failure still blocks a service-only change (deliberate).
