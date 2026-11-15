# Archive index

Material kept for provenance, frozen evidence or policy input. Nothing in this
directory is a statement about current behavior — `docs/ORIENTATION.md` and the
code are.

Rule: the content of these files is **not edited**. A banner may say what the
document is and where the current equivalent lives; the measurements and
conclusions stay exactly as recorded.

## Historical documents

| Path | Written for | Status | Why kept |
| --- | --- | --- | --- |
| `ORIENTATION-K6.1.md` | HEAD `4d71e5b`, K6.1 frozen | HISTORICAL | Describes the engine before verification profiles; superseded by `docs/ORIENTATION.md` |
| `phase-4.2.md` (this directory) | Phase 4.2 | HISTORICAL | Fast-path wiring and the three-way experiment design, with its measured numbers |
| [`../phase-5.0-design.md`](../phase-5.0-design.md) | Phase 5.0 | HISTORICAL (kept in place) | The scoped-evidence design that `scoping.py` and `evidence.py` grew from. It stays under `docs/` because `asha/scoping.py` names that exact path and `tests/test_watcher_stepper.py::test_governance_trio_frozen` pins the file's contents |
| `jspace-controller/` | pre-0.6 controller contract | LEGACY (removed component) | Documents `.jspace/control.py`, deleted when its gate moved to `asha/governance/ship_gate.py` |

## Frozen benchmark evidence (never regenerated)

| Path | Status | Notes |
| --- | --- | --- |
| `benchmarks/k5/` | BENCHMARK_OR_RESEARCH (frozen) | `FROZEN_SPEC.md`, `EXACT_COMMANDS.md`, `REPORT.md`, raw results |
| `benchmarks/k6/` | BENCHMARK_OR_RESEARCH (frozen) | Includes `CACHE_AUDIT.md` |
| `benchmarks/k6_1/` | BENCHMARK_OR_RESEARCH (frozen) | Includes `ROOT_CAUSE.md`, `IMPLEMENTATION.md` |
| `K5.3/` | BENCHMARK_OR_RESEARCH (frozen) | `REPORT.md` plus 80+ per-trial JSON results, hashed in `post_fix/manifest.json` |

`AGENTS.md` protects these artifacts: do not rewrite, regenerate or "fix" them.

## Other research and reference material (kept in place)

| Path | Classification | Why it stays where it is |
| --- | --- | --- |
| `benchmarks/live_eval/` | BENCHMARK_OR_RESEARCH | A/B campaign notes and the eval harness (`asha_eval/`) |
| `benchmarks/phase10/` | BENCHMARK_OR_RESEARCH | Phase 10 harness, raw runs and evidence |
| `benchmarks/data/` | BENCHMARK_OR_RESEARCH | Inputs referenced by the harnesses |
| `docs/audits/` | HISTORICAL RECORD | Integrity/prove report, revision 2 |
| `docs/reviewer/SENIOR_PYTHON_REVIEWER_SPEC.md` | ACTIVE POLICY INPUT | Distilled into `asha/governance/live_reviewer.py` and `semantic_review.py`; the code cites this file |
| `docs/demo.html`, `docs/assets/asha-demo.gif` | ACTIVE DOCUMENTATION | The README demo |

## Repository inventory snapshot (2026-11-15)

Top-level directories of `xenups/Asha` at the cleanup branch base, classified:

| Path | Classification | Evidence |
| --- | --- | --- |
| `asha/` | CORE | 82 shipped modules (`pyproject.toml` includes `asha*`) |
| `tests/` | TESTS | 120 files; the release gate |
| `docs/` | ACTIVE_DOCUMENTATION + archive | linked from the README |
| `skills/` | ACTIVE RESOURCE | `asha/worker_policy.py` resolves `skills/<name>/SKILL.md` |
| `scripts/` | ACTIVE TOOLING | bootstrap / update / uninstall / env checks |
| `.github/workflows/` | ACTIVE CI | `ci.yml`, `asha-governance.yml` |
| `.claude/commands/` | INTEGRATION (optional) | One command file that shells out to the CLI |
| `benchmarks/` | BENCHMARK_OR_RESEARCH | frozen + reproducible experiments |
| `K5.3/` | BENCHMARK_OR_RESEARCH (frozen) | K5.3 trial evidence |
| `.jspace/` | LEGACY STATE DIR | Only `dependencies.json`; the controller it served is deleted |
| `modules/`, `SKILL.md` (root) | LEGACY | Contract of the deleted controller; archived (see `jspace-controller/`) |

Duplicate scan: no byte-identical tracked files outside the frozen benchmark
sets; the only repeated content is benchmark JSONs that differ by trial id.

Wheel contents: `pyproject.toml` ships `asha*` only, so `docs/`, `skills/`,
`scripts/`, `tests/`, `benchmarks/`, `K5.3/` are repository-only by design —
their absence from the wheel is not evidence that they are unused.
