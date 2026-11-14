# Environment diagnosis: who failed, the code or the machine

A mandatory check (pytest, ruff, mypy) can fail for a reason that has nothing
to do with the worker's change: its tool or the project's dependency is not
installed in the interpreter Asha used, or the suite never collected. Both
cases produce the same non-zero exit code, so the exit code alone cannot be
used to blame the code.

## Interpreter selection

`asha.governance.env_resolver` picks the interpreter for each service root:

1. an explicit policy/configuration override;
2. a service-local environment (`<service>/.venv`, `venv`, `env`, conda);
3. otherwise the interpreter Asha itself is running under — **non-hermetic**,
   and only allowed while `ASHA_ALLOW_SYSTEM_PYTHON_FALLBACK` is not `false`.

Step 3 is a fallback, not a guarantee: Asha's own dependencies are not the
target project's dependencies. Nothing is installed on the project's behalf.

Every check entry records the environment that ran it, so a comparison across
two environments can be detected instead of silently trusted:

```json
{"name": "pytest", "status": "failed", "exit_code": 2,
 "interpreter": "/repo/svc/.venv/bin/python", "hermetic": true,
 "service_root": "/repo/svc", "failure_mode": "missing_dependency",
 "failure_module": "yaml"}
```

## Classifications

`asha.governance.check_diagnosis` classifies each failed entry from its own
output. It decides nothing and seals nothing:

| `failure_mode` | meaning | attributed to |
|---|---|---|
| `environment_unresolved` | the service environment never resolved | environment |
| `missing_dependency` | a non-stdlib, non-repository module is missing | environment |
| `tool_unavailable` | ruff/mypy/pytest itself is not installed | environment |
| `collection_error` | the suite could not be collected | neither |
| `test_failures` | parseable `FAILED` identities exist | the code |
| `timeout` | the check exceeded its budget | neither |
| `crashed` | the process never launched or its output was lost | neither |
| `unknown` | no parseable identity | never a pass |

A module that is standard library, or that lives in the repository (top-level
package or under `src/`), is a **collection error**, not a missing
dependency — a typo in an import is the code's problem.

## What the governed run reports

* A baseline that never exercised the code yields
  `comparison_inconclusive:baseline_missing_dependency` (or
  `baseline_tool_unavailable`, `baseline_environment_unresolved`) plus
  `;hint=...` naming the missing module, the interpreter that lacked it and
  the fix. `performed` stays `false` and no seal is written.
* A baseline and candidate that ran in different interpreters yield
  `INTERPRETER_MISMATCH` with `interpreter_drift`; the delta is never
  computed across environments.
* An acceptance command that exits `2` (interrupted / collection error) or
  `3` (internal error) is `inconclusive`, not `failed`: the runner produced
  no verdict. `4`/`5` stay `inconclusive`. `1` means the tests ran and failed.
  Every non-`passed` outcome still keeps the run out of `DONE` and leaves it
  unsealed.

## Locked git index

`git add -A` waits up to `asha.worktree.INDEX_LOCK_WAIT_S` (3s) for a
transient `index.lock` — a worker's own git command usually releases it.
Beyond that Asha refuses and reports the exact lock path with recovery
instructions. It never deletes a lock file: it cannot prove the owning
process is gone, and removing a live lock corrupts the index.
