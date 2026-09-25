---
description: Run the Asha governance CLI on the current repository and report its output verbatim
---

Run the Asha CLI against the repository containing `$ARGUMENTS`, using
the arguments the user supplied (default: auto-discovered change set).

```bash
asha $ARGUMENTS
```

If `asha` is not on PATH, use the module form from the repository root:

```bash
python -m asha $ARGUMENTS
```

Rules for this command (it is a thin passthrough, nothing more):

- Pass flags through unchanged (`--json`, `--no-execute`, `--paths …`,
  `--root …`, `--no-color`). Do not add, remove, or rewrite flags.
- Do not inspect graphs, compute git diffs, or interpret governance
  state yourself. Print the CLI's stdout as-is and its stderr as-is.
- Exit codes come from the CLI contract: 0 success / NO_CHANGES,
  1 validation failed, 2 operational error (including merge-conflict
  state or targets outside the repository), 130 interrupted.
- With `--json`, the stdout is exactly one JSON document; surface it
  unmodified.
