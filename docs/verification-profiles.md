# Verification profiles (0.7.4)

A **verification profile** is an operator-signed statement of which checks are
authoritative for a repository and its services. It replaces the
repository-agnostic scope matrix (`scope_resolver.SCOPE_CHECKS`) only where it
applies, and it is the only way a check can be recorded as
`NOT_CONFIGURED`.

Without a selected profile nothing changes: S2/S3/S4 still demand
`ruff + pytest + mypy`, a missing tool is still `skipped`, and a skip is still
`BLOCKED`.

## Selecting a profile

```bash
export ASHA_VERIFICATION_PROFILE=/etc/asha/profiles/detailist.json
asha run --spec task.json          # or the MCP tools, same behaviour
```

- Selection is **environment-only**. A task spec field named
  `verification_profile` is ignored: a worker cannot widen or narrow its own
  matrix.
- The file must live **outside the repository** it verifies (operator
  configuration, e.g. `/etc/asha/profiles/`). A profile inside the tree would
  let a candidate rewrite its own acceptance.
- Loading, identity verification and the digest snapshot happen **before the
  first check runs**; the file is re-read before sealing and any byte change
  fails the run (`profile changed during the run`).

## Schema (v1)

```json
{
  "schema": 1,
  "id": "detailist",
  "repository": {
    "remotes": ["github.com/void-detailist/detailist"],
    "ci_workflows": [".github/workflows/ci_compiler.yml"]
  },
  "services": {
    "compiler": {
      "root": "compiler",
      "python": "compiler/.venv/bin/python",
      "timeout_s": 900,
      "checks": {
        "pytest": ["-m", "pytest", "tests", "-q"]
      },
      "not_configured": {
        "ruff": "no [tool.ruff] in any pyproject; CI runs no ruff (audited 2026-11-14)",
        "mypy": "no mypy config or CI step (audited 2026-11-14)"
      }
    }
  },
  "provenance": {"declared_by": "operator", "evidence": ["ci_compiler.yml"]}
}
```

| field | meaning |
|---|---|
| `schema` | must be `1` |
| `id` | profile identity, recorded in evidence |
| `repository.remotes` | repository identities this profile binds to. `git@host:o/r.git`, `https://host/o/r` and `ssh://…/o/r.git` are one identity |
| `repository.ci_workflows` | CI files the declarations were derived from (recorded, not executed) |
| `services.<label>.root` | repo-relative service root; `<label>` must match the runner's service label (`.` for the repository root) |
| `services.<label>.checks` | **REQUIRED**. `name → argv after the interpreter` (`["-m", "pytest", …]`). The declared timeout applies per service |
| `services.<label>.not_configured` | **NOT_CONFIGURED**. `name → provenance`. Never inferred from a missing executable |
| `services.<label>.python` | optional interpreter path (repo-relative); defaults to the service's resolved interpreter |
| `provenance` | free-form record of who declared it and on what evidence |

A name may not be both required and `not_configured`; an empty profile, an
unknown schema, a missing remote list or a service with no required checks is
rejected.

## Verdicts

| verdict | meaning |
|---|---|
| `PASS` | every required check ran and passed. `NOT_CONFIGURED` entries are reported in the rule text and never counted as passes |
| `FAIL` | a required check ran and failed |
| `BLOCKED` | a required check could not run (missing tool, unlaunchable, skipped, timed out) — **or** the inventory contains no required check at all |
| `NOT_CONFIGURED` (aggregate) | no checks were recorded at all |

`NOT_CONFIGURED` per check is deliberately *not* a skip: a skip says "this was
required and could not run"; the profile entry says "this project does not gate
on it", with the provenance someone signed.

## What is recorded

The sealed worker evidence gains one field:

```json
"verification_profile": {
  "id": "detailist",
  "schema": 1,
  "digest": "9f2c…",
  "source": "/etc/asha/profiles/detailist.json",
  "remotes": ["github.com/void-detailist/detailist"],
  "ci_workflows": ["…"],
  "provenance": {"…": "…"},
  "services": {"compiler": {"required": ["pytest"],
                            "not_configured": ["mypy", "ruff"],
                            "timeout_s": 900}}
}
```

Worker evidence still seals `authorized_to_ship: false`: ship authority stays
with the ship gate, which re-derives it from that gate's own evidence chain
(`gate_states.GateDecision.authorized_to_ship`). A profile changes *which
checks are required*, never who authorizes shipping.

## Fail-closed rules

- profile file missing / unreadable / invalid JSON / empty → run refuses
- repository origin not listed in `repository.remotes` → refuses
- a diff reaching a service the profile does not declare → refuses (no
  fallback to the scope matrix: one run, one matrix)
- profile edited after the snapshot → refuses at seal time
- no required check left in the inventory → `BLOCKED`

## Scope interaction

Profiles do not relax the scope resolver, and they do not add S3 to
`scoping.ALLOWED_SCOPE_LEVELS`. A profiled run is simply not narrowed to
changed files: the profile's checks are complete by declaration, so scoped
validation is skipped (`decision.eligible` is ignored) and the declared
commands run for every changed service.
