---
name: asha-update
description: "Trigger the Asha-Harness atomic self-update. Use when the user says update asha, /asha update, or update the harness/dependencies."
---

# A-SHA UPDATE

The atomic, fail-closed self-update mechanism for Asha-Harness and its
registered dependency repos.

## Trigger

- User: "/asha update", "update asha", "update the harness", "update asha / dependencies"
- Run exactly: `.hermes/venv/Scripts/python.exe scripts/update.py` (Windows) /
  `.hermes/venv/bin/python scripts/update.py` (POSIX) — or the wrappers
  `scripts/update.sh` / `scripts/update.ps1`.
- Inspect first with `--dry-run`; it fetches and reports incoming commits
  without mutating anything.

## Protocol (fail-closed at every boundary)

1. **Clean tree guard** — `git status --porcelain` must be empty; dirty tree ⇒
   exit 2/1 with REFUSED message. Never update over uncommitted work.
2. **Fetch & inspect** — `git fetch <remote> <branch>`; no new commits ⇒
   "Asha is already up to date." exit 0.
3. **Fast-forward only** — `git merge --ff-only <remote>/<branch>`. A diverged
   history (non-ff) is refused, never merged.
4. **Dependency & ABI audit** — `code_search.py --verify-env` (pinned
   tree-sitter 0.21.3 / 1.10.2 / ast-grep-py 0.45.3), `ruff check .`,
   `pytest tests/ -q`.
5. **Rollback on gate failure** — any red gate ⇒ `git reset --hard HEAD@{1}`
   then exit 1 with the exact failure reason. The repo never stays on a
   failing update.

## Logging into the ledger

After a successful update, record the operation in `.jspace/control.json`
through the controller (never hand-edit):

```text
python .jspace/control.py --transport local pulse --event tool --label "asha-update: merged <old>..<new>, gates green"
python .jspace/control.py --transport local note --add "Asha-Harness updated to <new>"
```

Use the transport declared in the active session (`--transport ssh` when the
update runs on the remote side).

## Submodules

`.jspace/dependencies.json` declares `self` (origin/main, pinned) plus an
optional `submodules` list, each with `name`, `remote`, `branch`, and an
optional `test_command`. Submodules get the same fetch → ff-only → audit →
rollback treatment.

Fail-closed rule: if any step is red, the update is NOT applied and the tree
is rolled back. Say so plainly.