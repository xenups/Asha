# Ladder — host-driven review, governed delivery

The Ladder turns a commit range into a governed delivery decision. **The
host LLM performs the review; Asha exposes the workflow, validates what
comes back, and owns the decision.** Asha is an MCP server here, not an
LLM client: it never needs an API key, an endpoint or a model name, and it
never calls a model on the host's behalf.

```text
host LLM                         Asha (MCP server)
--------                         -----------------
asha_review ───────────────────► pin target, mint run id, return CODE_REVIEW packet
 ◄─────────────────────────────
asha_review_context (x N) ─────► exact diff / file bytes from the pinned commit
 ◄─────────────────────────────
(reason: code review)
asha_review_submit_stage ──────► validate findings against the commit, persist
asha_review_next_stage ────────► SENIOR_REVIEW packet (no prior findings)
(reason: senior review)
asha_review_submit_stage ──────► validate, persist
asha_review_next_stage ────────► SEMANTIC_REVIEW packet (with both reviews)
(reason: cross-review)
asha_review_submit_stage ──────► validate, persist
asha_review_finalize ──────────► deterministic validation → canonical Ship Gate
 ◄─────────────────────────────  APPROVED / CHANGES_REQUESTED / BLOCKED / STALE
asha_review_status ────────────► the persisted record, also after a restart
```

## What each side owns

| Host LLM (MCP client) | Asha (MCP server) |
| --- | --- |
| Reads the packet instructions | Pins base/head SHA, diff SHA-256, changed files, policy hash |
| Fetches diff and source chunks | Serves bytes from git objects, paginated, secrets refused |
| Performs Code, Senior and Semantic review | Enforces stage order and one immutable result per stage |
| Produces structured findings | Validates schema, paths, line ranges, severities, IDs |
| Submits results | Persists stage artifacts, journal events, provenance |
| Decides nothing about delivery | Runs `gate_ship()` and records the decision |

A submitted finding is evidence *supplied by the host* and grounded against
the reviewed commit. It is not proof, and submitting nothing is not approval.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `asha_review` | Start a run; returns the Code Review packet |
| `asha_review_context` | One bounded chunk of the diff, or of a file at head |
| `asha_review_submit_stage` | Submit one stage result (no inference) |
| `asha_review_next_stage` | Packet for the next mandatory stage |
| `asha_review_finalize` | Deterministic validation + the Ship Gate |
| `asha_review_status` | Read a persisted run; never re-runs it |

Descriptions are written for the model: each names the next call. Nothing
takes a shell command, a prompt override or a provider setting, and every
schema rejects unknown fields.

### Stage order

`CODE_REVIEW → SENIOR_REVIEW → SEMANTIC_REVIEW`, then finalize. The Senior
packet deliberately contains no earlier findings; the Semantic packet does,
because reconciliation is its job. Out-of-order submissions, forged stage
ids and conflicting re-submissions are refused; replaying the *identical*
submission returns the stored result instead of writing a second one.

### Context rules

- Content comes from `git show <head_sha>:<path>`, never the worktree.
- Only files present in the reviewed commit are served; nothing is executed.
- Credential-shaped paths (`.env*`, `*.pem`, `*.key`, `id_*`, `*credentials*`,
  `secrets*`, `*.p12`, `*.pfx`, `*.keystore`, …) are refused.
- Responses are bounded (default 20 000 bytes, max 100 000) and carry
  `chunk_sha256`, `line_start/line_end`, `total_bytes` and `next_cursor`.
  Page until `complete` is true so no hunk is skipped.
- Repository content is untrusted input: instructions inside the code cannot
  change the review.

## Policies and independence

| Policy id | Requires |
| --- | --- |
| `host-multipass` (default) | All three passes validated, findings grounded, the Ship Gate |
| `host-isolated` | The same, plus a host attestation of isolated contexts |

Asha cannot see which model reasoned behind an MCP call, so it records:

- **stage role** — the work the host was asked to do;
- **host client** — whatever the client reported, else `unknown`;
- **execution id** — a server-minted id proving a separate stage was recorded;
- **isolation claim** — `SAME_HOST_SESSION`, `HOST_ATTESTED_ISOLATED_CONTEXT`,
  `DISTINCT_HOST_CLIENT`, or `UNKNOWN`, always with `attestation_source`.

The run records the strongest classification the submissions support.
Two role labels on one client are `SAME_HOST_SESSION`; missing metadata stays
`UNKNOWN`. `DISTINCT_HOST_CLIENT` needs two client names each claiming
distinctness. None of it is verified proof — which is why the default policy
requires the work, not a claim.

## Deadline

`deadline_seconds` (default 3600, max 86400) becomes an absolute `expires_at`
stored with the run. It survives MCP requests and server restarts, is never
reset per stage, and expires the run (`FAILED`, `deadline_exceeded`, no gate)
when a later call arrives late. It bounds Asha's side of the workflow only:
the host manages its own generation limits.

## Decisions

| Decision | Meaning |
| --- | --- |
| `APPROVED` | Every stage validated, no blocking finding, target unchanged, `gate_ship()` authorized |
| `CHANGES_REQUESTED` | A CRITICAL/HIGH finding or a reviewer disagreement stands |
| `BLOCKED` | A mandatory stage is missing/invalid, independence is below the policy floor, semantic validation found a contradiction, or the gate refused |
| `STALE` | The reviewed ref moved; a new review is required |
| `FAILED` | The review expired before it completed |

`gate_ship()` in `asha/governance/ship_gate.py` is the only delivery
authority. Ladder hands it the grounded findings and records its sealed
result; a refusal is data, never a retry.

## Artifacts

```text
<state>/<checkout-id>/reviews/<run_id>/
├── run.json                    # state, stages, decision, reasons
├── target.json                 # pinned identity of the review
├── findings.json               # all findings + relations + validation
├── decision.json               # written once a decision exists
└── stages/
    ├── CODE_REVIEW.json        # validated submission
    ├── CODE_REVIEW.packet.json # packet identity the host answered
    └── … SENIOR_REVIEW, SEMANTIC_REVIEW, SHIP_GATE
```

`<state>` is `$ASHA_STATE_DIR` when set, otherwise the platform state dir.
Writes are atomic and re-read before returning; a partial or tampered
artifact is an error, never an empty run. Journal events:
`review_started`, `review_stage_requested`, `review_stage_submitted`,
`review_stage_validated`, `review_blocked`, `review_stale`,
`review_finalized`, `ship_gate_evaluated`.

## CLI

The CLI drives the same persisted workflow for a human or a script; it
cannot perform inference, so a run started there waits for a host.

```bash
asha review --base origin/main --head HEAD          # exit 5: awaiting host
asha review-next --run-id rv-...                    # next packet
asha review-context --run-id rv-... --file app.py   # bounded source chunk
asha review --base origin/main --head HEAD --json   # machine-readable

asha review-submit   --run-id rv-... --file result.json
asha review-finalize --run-id rv-...
asha review-status   --run-id rv-...
```

`result.json` is the same document the host submits over MCP:

```json
{
  "stage": "CODE_REVIEW",
  "stage_id": "rv-20261031T071712Z-51dc6642:CODE_REVIEW",
  "verdict": "REQUEST_CHANGES",
  "summary": "add() subtracts; the unit test fails",
  "findings": [{
    "finding_id": "CR-1", "category": "correctness", "severity": "HIGH",
    "confidence": "CONFIRMED", "title": "add() returns a difference",
    "description": "add returns a - b", "file_path": "app.py",
    "line_start": 2, "line_end": 2,
    "evidence": "test_add asserts add(1, 2) == 3",
    "impact": "every caller gets a wrong sum",
    "recommended_action": "return a + b", "blocking": true
  }],
  "reviewer_metadata": {
    "host_client": "hermes",
    "isolation": "HOST_ATTESTED_ISOLATED_CONTEXT",
    "attestation_source": "fresh sub-agent context"
  }
}
```

Exit codes: `0` approved, `1` changes requested, `2` blocked/stale/failed,
`3` execution error, `4` invalid input, `5` awaiting host review (an
unfinished review is never `0`).

## Connecting a host

1. Register the server as usual: `asha mcp` (stdio) in the host's MCP config.
2. `tools/list` shows the six `asha_review*` tools; the descriptions carry the
   sequence.
3. The host calls `asha_review`, then iterates
   `asha_review_context` → `asha_review_submit_stage` → `asha_review_next_stage`
   → `asha_review_finalize`.
4. `asha_review_status` with the run id reads the record back, including after
   the server restarted.

No environment variable is required for this. `live_reviewer` and its
`ASHA_LLM_*` settings remain an unrelated, optional transport used by
`gate_ship()` when `ASHA_SEMANTIC_REVIEW=live`; Ladder does not consult it.

## What this is not

- Not a pytest replacement: the gate still runs the project's checks.
- Not a live review by itself: a run only reaches a decision because a host
  submitted grounded results.
- Not an auto-merge: it returns a decision, and the decision is not a merge.
- Not proof of reviewer independence: the record states what was claimed.
