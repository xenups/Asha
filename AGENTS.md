# AGENTS.md — Operational Contract for Coding Agents

This is a contract, not a tutorial. Violations block or invalidate work.
Applies to any agent (manual or automated) working in this repository.

## Repository / task boundaries

1. Work only within the scope declared for the task. Scope is defined by
   the task's declared paths, and enforced by `scope_resolver` /
   `scoping_decision` at execution time. Assume it is enforced.
2. Know which repository you are in and which branch before any command:
   always run `git branch --show-current` first. Never run commands in the
   wrong checkout.
3. Production code, tests, benchmarks, and docs are separate surfaces.
   Benchmarks marked FROZEN (`benchmarks/k6/FROZEN_SPEC.md`,
   `benchmarks/k6_1/FROZEN_SPEC.md`) must never be modified, re-ran into
   new results, amended, or re-committed after freezing.

## No unauthorized file modifications

4. Do not modify files outside the task's stated scope. If a fix requires
   touching something out of scope, stop and escalate before editing.
5. Do not modify frozen benchmark artifacts, their `results.json`,
   `manifest.json`, or `REPORT.md` retroactively. New experiments get a new
   versioned directory.

## Test integrity

6. Never weaken, delete, skip, or mock-pollute a test to obtain green
   results. A green suite obtained by editing the tests is not green.
7. Never add a mock that changes what the code under test actually does.
   Mocks that isolate a boundary are acceptable; mocks that change the
   outcome are not.
8. Every bug fix requires a regression test that fails on the unfixed code
   and passes on the fix. No regression test, no fix.

## Git safety

9. No force-push. No `git reset --hard`, `git clean -fd`, or forced
   checkout unless explicitly authorized for that exact operation.
10. Never amend or rewrite an existing commit, including an earlier phase's
    commit, without explicit authorization. Each fix is a separate commit
    so it can be selectively reverted.
11. Stage only files that belong to the current task. Inspect
    `git diff --cached` before committing; unrelated files stay out.
12. The user merges PRs and decides branch/PR ordering. Do not merge
    on their behalf. `git push` to the task's own branch is allowed where
    the workflow requires it; pushing straight to shared branches follows
    the specific task instructions.

## Validation integrity

13. Baseline → mutation → current verification must be **cache-safe**:
    the pipeline must strip verification caches (`_strip_check_caches`:
    `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`) between
    baseline and current verification, or the current run can reuse stale
    `.pyc`/mtime bytes and mask a regression. Never disable or bypass this
    to make a run pass.
14. Never claim a verification result you did not observe. Report exact
    commands, exit codes, counts, and CI run IDs. "It should pass" is not
    a result.
15. A suite with a known pre-existing failure is not green: report the
    failure separately and never misrepresent it as passing.

## Context retrieval contract

16. **Grep for candidate discovery.** Find candidate files/symbols with
    grep/search tools. This is the cheap, complete discovery layer.
17. **`slice_context` for targeted context.** For a specific symbol, build
    the agent payload with `slice_context` (target source + dependency
    stubs + completeness state). Do not dump whole files when a slice is
    available.
18. **Qualified method names.** Methods are indexed as
    `ClassName.method`. Use `FailureIdentity.from_dict`,
    `DAGCoordinator.run` — bare `from_dict` / `run` do NOT resolve and
    yield `UNKNOWN`.
19. **COMPLETE is scoped.** `COMPLETE` means complete under the slicer's
    declared policy, traversal bounds, and available graph — never
    repository-wide semantic completeness. Proceed on a COMPLETE slice,
    but do not claim repository-wide coverage.
20. **INCOMPLETE escalates.** A slice marked `INCOMPLETE` (budget
    exhausted, unresolved nodes, or zero resolved deps — check
    `completeness_reasons`) requires supplementary investigation. Do not
    act as if the context were complete; expand the search (grep, larger
    `max_nodes`, forward + reverse) and re-slice.
21. **UNKNOWN is not SAFE and not COMPLETE.** Keep UNKNOWN unresolved.
    Never treat absent context as evidence of absence. If verification
    depends on context you cannot obtain, block and report rather than
    guessing.

## Failure-evidence and root-cause protocol

22. On a failure: identify the failure identity (`FailureIdentity` —
    check, location, code, message), separate pre-existing (in baseline)
    from new (`delta_failures`), and confirm the verdict
    (`verdict_for`). Unparseable-but-failed checks are UNKNOWN, never
    clean.
23. Evidence before changes: reproduce, then trace, then fix. Establish
    root cause with source-level evidence before modifying production
    code. If the root cause is unproven, say exactly what is proven and
    what is not.
24. Preserve failure evidence (`persist_failure_evidence`) rather than
    deleting traces of the failure.

## Completion report requirements

25. Report: branch, exact commit hash(es), files changed, tests run
    (exact commands + counts), lint/type results, CI run ID and status,
    and any pre-existing failures encountered.
26. Report what was NOT performed (e.g. agent-in-the-loop validation,
    full suite, server sync) as explicitly not performed.
27. Never report a push, CI pass, deployment, or health check as
    successful unless its result was actually observed.