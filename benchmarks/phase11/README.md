## Post-merge verification addendum — 2026-11-01 (Phase 11)

Everything above is the record of the benchmark as it was run, before the fixes
below existed. Nothing above is rewritten. Five layers are kept distinct.

### 1. Original benchmark observations (unchanged)

Three matched pairs, six primary executions, six acceptance passes, Asha
authorization **0 of 3**, wall clock **2.51×**, model calls **2.81×**, tokens and
dollar cost **Not measured**, blind review preferring the Asha condition in all
three pairs, and the independence limitation `SAME_HOST_SESSION`. The original
S6 control stays invalid; the corrected S6b is recorded separately.

The `0/3` figure still describes what the benchmark measured and is **not**
retroactively changed by any fix.

### 2. F1 — target-tree identity (merged: PR #20, `5dcef68`)

Confirmed bug, not expected SHA-pinned behaviour. A review whose head was pinned
to a commit id could not observe the branch moving, so finalize authorized a tree
the review had never seen. Fixed by comparing the reviewed commit's tree with the
checked-out tree. Same content at a different commit stays reviewable.

Verified on integrated `main` (`bdbcc42`) through a real MCP server, move-then-
finalize order: SHA-pinned + changed tree → `STALE`, gate `NOT_EXECUTED`; SHA-
pinned + identical tree → `APPROVED`; ref moved → `STALE`; annotated tag + changed
tree → `STALE`; detached HEAD at the reviewed commit → `APPROVED`; tracked and
untracked local edits → `REFUSED`.

### 3. F2 — failure diagnostics (merged: PR #21, `695723c`)

Confirmed diagnostic insufficiency, not a wrong verdict. An inconclusive
comparison named the baseline while the candidate's own ruff syntax errors sat in
a sibling field. The reason now carries a bounded digest of the candidate failures
and the worker command's exit status. Classification, state and fail-closed
outcome are unchanged; the refusal that was already correct stays correct.

### 4. F3 — target-project test side effect (not an Asha defect)

The untracked `root_network_plan_planned.yaml` is written by the **target
repository's** suite: `pydetactl/tests/test_plan.py` invokes the `plan` CLI
without `--output-path`, and `finalize_command_result` resolves the output
directory from the working directory. Reproduced in a clone that never contacts
Asha. The fix belongs there and is proposed as a separate target-project change
(pytest `tmp_path`), not as an Asha change. Asha's refusal of that tree was
correct: the tree held content no review had seen.

### 5. Integrated-main verification and a new finding (F4)

On the merged tree the full suite passes and every F1/F2 scenario behaves as
required, but a live MCP run surfaced a **separate governance defect**:

* a commit that adds a lint-clean failing test under `pydetactl/tests` was
  authorized with `gate_state=COMPLETED_CLEAN`;
* the gate's own evidence for that run records `pytest ... exit_code 0,
  ".  1 passed in 0.01s"` — one test, from the repository-root `tests/`
  directory. The service suite was never collected.

`asha/check_runner.py` passed `tests/` whenever the checkout root had such a
directory, so in a monorepo the suite beside the change never ran while the
evidence said "pytest passed". This is not caused by F1 or F2 and it predates
them; it is recorded here because this addendum must not imply that a green
integrated suite means the gate's evidence is complete.

The `0/3` benchmark result is unaffected: those runs were blocked by a dirty
tree, before the check matrix had a chance to matter.
