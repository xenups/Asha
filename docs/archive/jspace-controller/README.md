# J-Space controller contract (legacy)

These two files are the contract of a component that **no longer exists in this
repository**: `.jspace/control.py`, the "J-Space SV1 cooperative controller".

They are kept because they explain the pre-0.6 architecture and the origin of
today's gate semantics; they are not documentation of current behavior.

## Why this is legacy, with evidence

| Claim | Evidence |
| --- | --- |
| `control.py` is gone | `git log --diff-filter=D -- .jspace/control.py`; the gate it implemented now lives in `asha/governance/ship_gate.py`, whose docstring says it was "extracted from the deleted `.jspace/control.py`, H.1" |
| Nothing loads these files | no reference in `asha/`, `tests/`, `scripts/`, `.github/`; `asha/worker_policy.py` resolves worker Skills from `skills/<name>/SKILL.md`, not from the repository root |
| The module list is incomplete | `SKILL.md` names `modules/capacity.md`, `modules/broadcast.md` and `modules/orchestration.md`; only `modules/self-monitoring.md` was ever committed, so the read-gate it describes could not be satisfied from this tree |
| The bootstrap gate was broken by the deletion | `scripts/bootstrap.sh` and `bootstrap.ps1` ran `mypy … .jspace/control.py …`; with the file gone that gate failed. Fixed in the same commit that moved these documents (the gate now covers `asha/` and `tests/` only) |
| `.jspace/` itself is still used | `scripts/update.py` reads `.jspace/dependencies.json`; the control lock the gate used lives in the state dir (`$ASHA_STATE_DIR/lock`), not there. The file remains in place and is untouched |

## What to read instead

- `asha/governance/ship_gate.py` — the surviving gate, same refusals.
- `docs/ORIENTATION.md` — current module map and invariants.
- `docs/archive/INDEX.md` — the rest of the archived material.
