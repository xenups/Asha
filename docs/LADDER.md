# Ladder — governed review of an exact commit range

Ladder is Asha's review pipeline. It turns "the tests pass" into a review
that a delivery decision can be replayed from:

```text
exact base..head commits
   -> Code Review        (isolated provider call)
   -> Senior Review      (separate, isolated provider call)
   -> Semantic validation of both passes
   -> canonical Ship Gate (asha/governance/ship_gate.py)
   -> APPROVED / CHANGES_REQUESTED / BLOCKED / STALE
```

Each stage is recorded separately, so "review ran", "review approved" and
"delivery allowed" stay three different facts. Missing review is never a
passing review.

## What it is not

- Not a replacement for `pytest` or the existing gates: it runs *on top*
  of the same commit that CI already tests.
- Not a live review unless a provider is configured. With no provider the
  run is `BLOCKED` and says which variable is missing.
- Not an auto-merge. It never merges, tags, releases or deploys.

## Configure a provider

Ladder reuses the existing live-reviewer configuration — the same variables
the semantic reviewer already uses:

| Variable | Meaning |
| --- | --- |
| `ASHA_LLM_API_KEY` | API key for an OpenAI-compatible endpoint (required) |
| `ASHA_LLM_BASE_URL` | Base URL of the endpoint |
| `ASHA_LLM_MODEL` | Model name reported in provenance |
| `ASHA_SEMANTIC_REVIEW` | Enables the semantic-review stage elsewhere |

Check whether they are present without printing values:

```bash
for name in ASHA_LLM_API_KEY ASHA_LLM_BASE_URL ASHA_LLM_MODEL; do
  printenv "$name" >/dev/null && echo "$name=set" || echo "$name=unset"
done
```

If the key is unset, `asha review` still works — it returns `BLOCKED`:

```text
CODE_REVIEW   BLOCKED   ASHA_LLM_API_KEY is not set: Ladder needs a
                        configured review provider...
```

## Run a review

Reviews are of committed objects only. Commit first; a dirty worktree is
refused rather than silently reviewed.

```bash
asha review --base origin/main --head HEAD
asha review --base origin/main --head HEAD --json
asha review --base origin/main --head HEAD --deadline-seconds 900
asha review --base origin/main --head HEAD --policy distinct-provider
```

The whole run shares one deadline: every stage gets
`min(per-operation budget, what is left)`. A stage never launches after the
deadline, and an exhausted deadline is `NOT_EXECUTED`, not a pass.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | `APPROVED` |
| 1 | `CHANGES_REQUESTED` — blocking finding or reviewer disagreement |
| 2 | `BLOCKED` or `STALE` — a precondition (provider, stage, source) failed |
| 3 | execution error (bad ref, dirty tree, unreadable artifact) |
| 4 | invalid arguments (unknown policy, bad flag value) |

## Inspect a previous review

```bash
asha review-status --run-id rv-20261031T045325Z-63013c82
asha review-status --run-id rv-... --json
```

It re-reads the persisted artifacts. It never re-runs a stage and never
fills a gap with a guess: an unknown id exits 3.

## MCP

The same service behind two tools (`TOOL_SPECS` in `asha/mcp_server.py`):

- `asha_review` — `{root?, base?, head?, deadline_seconds?, policy?}`
- `asha_review_status` — `{root?, run_id}`

Inputs are refs and a policy id only. No command execution, no prompt
override, no way to skip a mandatory stage. A blocked review is a normal
result (`isError: false`, `decision: "BLOCKED"`); malformed arguments are
tool errors.

## Stages, statuses and meanings

Execution status: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `BLOCKED`,
`NOT_EXECUTED`, `STALE`.

Review verdict: `APPROVE`, `REQUEST_CHANGES`, `INCONCLUSIVE`,
`NOT_APPLICABLE`.

Decision: `APPROVED`, `CHANGES_REQUESTED`, `BLOCKED`, `STALE`.

A provider reply of `REQUEST_CHANGES` is a `COMPLETED` stage with a
non-approving verdict. A missing provider is `BLOCKED`. Malformed output is
`FAILED`. Only `APPROVED` means delivery.

## Findings

Every reviewer must return one JSON object:

```json
{"verdict": "REQUEST_CHANGES",
 "findings": [{"severity": "HIGH", "confidence": "CONFIRMED",
               "category": "correctness", "title": "...",
               "description": "...", "file_path": "app.py",
               "line_start": 2, "line_end": 2, "evidence": "...",
               "impact": "...", "recommended_action": "...",
               "blocking": true}]}
```

Asha validates each finding before it becomes evidence: path must be in the
reviewed change set and repository-relative (no traversal), lines must
exist in the head commit, severity/confidence must be known values. A
finding that fails validation fails the whole stage — the pipeline never
"salvages" partial reviewer output.

`severity` decides blocking (`CRITICAL`, `HIGH`); the reviewer's own
`blocking` flag is recorded as a claim, not as policy. `CRITICAL` maps to
the gate's `BLOCKER`; `INFO` to `LOW`.

## Independence

The two review passes are separate provider calls, each with its own
execution id, request, context hash and artifact. Neither receives the
other's findings on its first pass.

Recorded classification:

- `distinct_provider` — different providers (real independence),
- `same_provider_isolated_context` — same provider, separate request,
- `same_session` — same provider session,
- `unknown` — metadata missing; never upgraded.

Policies:

- `default` — requires `same_provider_isolated_context`.
- `distinct-provider` — requires `distinct_provider`; with one provider
  configured the run is `BLOCKED` and names the unmet requirement.

Different model names on one account are *not* independence.

## Artifacts

Reviews live outside the repository, next to the existing evidence and
journal:

```text
${ASHA_STATE_DIR:-~/.local/state/asha}/<checkout-id>/reviews/<run_id>/
    run.json         # the record review-status reads
    target.json      # base/head SHA, diff sha256, files, policy hash
    findings.json    # findings + relations (never merged or dropped)
    decision.json    # status, decision, reasons, gate result
    stages/<STAGE>.json
```

`run.json` carries `schema: asha-ladder/v1` and `artifact_schema: 1`. Files
are written to a temp file and `os.replace`d, then re-read and hashed;
`load_run` refuses a truncated or mis-versioned artifact rather than
returning an empty run. Nothing is written into the worktree.

Lifecycle events go to the existing journal: `review_started`,
`review_stage_completed`, `review_stage_blocked`, `review_stale`,
`review_decided`.

## Staleness

The run records the requested refs and the SHAs they resolved to. Before
the Ship Gate, both are re-resolved: if a ref moved or the diff hash
changed, the run is `STALE`, the gate is not executed, artifacts are kept,
and delivery needs a new run.

## Fail-closed conditions

`APPROVED` requires all of: every mandatory stage completed, valid
structured output from both reviewers, unchanged source identity,
independence at or above the policy floor, semantic validation completed,
no `CRITICAL`/`HIGH` finding, no unresolved reviewer disagreement, and the
canonical Ship Gate authorizing the ship.

Anything else is `CHANGES_REQUESTED`, `BLOCKED` or `STALE` — with the
reason naming the cause.

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| `BLOCKED` + `ASHA_LLM_API_KEY is not set` | no provider | export the key, rerun |
| `BLOCKED` + `unavailable: HTTP 401` | bad credentials | fix the key; the run is not a review |
| `FAILED` + `timeout:` | provider exceeded its budget | raise `--deadline-seconds` or shorten the diff |
| `FAILED` + `invalid_response:` | reviewer returned non-JSON or unknown severity/path | inspect `stages/CODE_REVIEW.json`; the model must follow the contract |
| `BLOCKED` + `--deadline-seconds` exhaustion | whole-run budget spent | rerun with a larger budget |
| `STALE` | branch moved mid-review | review the new head |
| `REVIEW ERROR: TargetError` | dirty tree, bad ref, or base == head | commit, fix the ref |
