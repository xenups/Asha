"""Ship-gate surface (extracted from the deleted .jspace/control.py, H.1).

Preserves the EXACT gate semantics that control.py `check --stage ship`
executed, factored onto the kept modules (evidence, check_runner,
scope_resolver, scoping) — no scheduler, no AST, no in-tree state.

Pre-D restoration (from Phase D.1): when the base is unresolved and the
affected-file set is empty, the full S2 equivalent suite (ruff, pytest,
mypy) runs instead of defaulting to the S0 subset, so committed runtime
files never skip lint silently (the legacy HEAD~1 fallback's effect).

Phase 1 hardening: authorization is now DERIVED from a typed gate state
(asha.gate_states) over an audit snapshot (asha.audit_snapshot) instead of
the bare predicate `all(status in (passed, skipped))`. That predicate was
fail-OPEN on an empty check inventory (`all([]) is True`), so a run that
executed nothing authorized shipping. The gate now also

  * binds the decision to the audited snapshot_id and re-derives it AFTER
    the checks finish, so a target that changed while checks ran is
    STALE_REVIEW and denied (TOCTOU);
  * refuses when the required-check inventory is empty, incomplete, or
    contains a failed check or a skip the runner did not justify with a
    proven empty target set (a skip WITH that proof stays non-failing,
    preserving the existing evaluator contract);
  * validates the evidence contract structurally (missing fields, unknown
    states, inconsistent inventories are INVALID_EVIDENCE, never success).

Existing evidence fields keep their shape; gate_state, snapshot_id and
manifest_version are ADDITIVE keys covered by the canonical digest.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from asha import audit_snapshot, check_runner, evidence, gate_states, scope_resolver
from asha.governance import semantic_review

# Policy/rule-set version the sealed decision is bound to. Bumped whenever
# the authorization rules below change, so an old artifact can never be
# read as if it were produced under the new policy.
# 'phase2': semantic reviewer evidence is mandatory; findings partition
# by severity (SEVERITY_BLOCKS_SHIP = {BLOCKER, HIGH}).
POLICY_VERSION = 'phase2'

# Budget handed to the injected semantic reviewer; a reviewer that blows
# it raises TimeoutError -> failed check entry -> EXECUTION_FAILED.
REVIEW_TIMEOUT_S = 900

# Injected executor contract: reviewer(root, snapshot_id, timeout_s)
# returns the semantic-review/v1 artifact (or raises / returns garbage,
# which fail closed -- see _run_reviewer).
ReviewerFn = Callable[[Path, str, int], dict]


def _run_reviewer(reviewer: ReviewerFn | None, root: Path,
                  snapshot_id: str, checks: list[dict]) -> dict | None:
    """Run the injected semantic reviewer; execution failures become check entries.

    Failure mapping (fail closed on every path):

    * reviewer is None            -> returns None -> missing artifact ->
      INVALID_EVIDENCE (policy requires a review).
    * timeout / crash             -> FAILED 'semantic-review' check entry +
      placeholder artifact (identity-valid, empty findings) so the failed
      CHECK decides: EXECUTION_FAILED.
    * non-dict output             -> same as crash: EXECUTION_FAILED.
    * dict with wrong/missing     -> returned as-is; validation raises ->
      snapshot identity / bad findings INVALID_EVIDENCE.

    The placeholder never carries findings -- it exists only so the
    phase2 identity contract is satisfiable while the appended failed
    check drives the denial.
    """
    if reviewer is None:
        return None

    def _placeholder(status: str, error: str) -> dict:
        return {
            'schema': semantic_review.REVIEW_SCHEMA,
            'snapshot_id': snapshot_id,
            'policy_version': POLICY_VERSION,
            'status': status,
            'error': error,
            'findings': [],
        }

    try:
        artifact = reviewer(root, snapshot_id, REVIEW_TIMEOUT_S)
    except (TimeoutError, subprocess.TimeoutExpired):
        checks.append({'name': 'semantic-review', 'status': 'failed',
                       'exit_code': -1,
                       'note': f'timed out after {REVIEW_TIMEOUT_S}s'})
        return _placeholder('timeout', 'reviewer timed out')
    except Exception as exc:  # any executor crash fails closed
        checks.append({'name': 'semantic-review', 'status': 'failed',
                       'exit_code': -1,
                       'note': f'crashed: {type(exc).__name__}'})
        return _placeholder('crashed', type(exc).__name__)
    if not isinstance(artifact, dict):
        checks.append({'name': 'semantic-review', 'status': 'failed',
                       'exit_code': -1,
                       'note': 'malformed reviewer output (non-object)'})
        return _placeholder('malformed', type(artifact).__name__)
    # A completed review is a POLICY check entry too (success path:
    # executed == required inventory stays equal under phase2).
    checks.append({'name': 'semantic-review', 'status': 'passed',
                   'exit_code': 0, 'note': ''})
    return artifact


def _gate_evidence(snapshot_id: str, resolved: dict,
                   checks: list[dict],
                   reviewer_artifact: dict | None) -> gate_states.GateEvidence:
    """Adapt the resolved inventory + executed results to the contract.

    Findings come exclusively from the reviewer artifact; under phase2
    both the artifact identity and the finding schema are validated
    inside validate_evidence (missing/wrong artifact -> INVALID).
    """
    required = list(resolved['checks'])
    if POLICY_VERSION == 'phase2' and 'semantic-review' not in required:
        # The review is part of the mandatory inventory under phase2.
        required.append('semantic-review')
    return gate_states.validate_evidence({
        'snapshot_id': snapshot_id,
        'evidence_schema': evidence.SCHEMA,
        'policy_version': POLICY_VERSION,
        'required_checks': required,
        'checks': [
            {
                'name': entry['name'],
                'status': entry['status'],
                'exit_code': entry.get('exit_code'),
                'note': entry.get('note', ''),
            }
            for entry in checks
        ],
        'findings': list((reviewer_artifact or {}).get('findings') or []),
        'semantic_review': reviewer_artifact,
    })


def gate_ship(root: Path, *, no_execute: bool = False,
              reviewer: ReviewerFn | None = None) -> dict:
    """Run the ship gate; returns the sealed evidence payload.

    Raises evidence.EvidenceError on refusal. `authorized_to_ship` is
    derived from the typed gate state -- it is never accepted as an input
    and never inferred from the absence of findings.

    ``reviewer`` is the injected semantic-review executor
    (``reviewer(root, snapshot_id, timeout_s) -> artifact``). Under
    POLICY_VERSION 'phase2' a review is MANDATORY: a missing executor
    fails closed (INVALID_EVIDENCE), and reviewer timeout/crash/malformed
    output map to a failed check entry (EXECUTION_FAILED). Findings
    (severity-partitioned by gate_states.evaluate) come only from the
    artifact, which is bound to the audited snapshot_id.
    """
    root = Path(root).resolve()
    # tamper check on any prior artifact BEFORE anything else
    evidence.verify(root)
    evidence.require_clean_tree(root)
    resolved = scope_resolver.resolve(root)
    # Pre-D restoration (D.1): unresolved base + empty diff -> full S2
    if resolved["base"] is None and not resolved["affected_files"]:
        resolved["checks"] = ["ruff", "pytest", "mypy"]
    # The audited source snapshot: taken BEFORE execution so a target that
    # changes while checks run is detectable at seal time.
    audited_id = audit_snapshot.snapshot_id_of(root)
    if no_execute:
        checks: list[dict] = []
        reviewer_artifact: dict | None = None
    else:
        checks = check_runner.run(root, resolved)
        # The reviewer runs inside the audited window: before the
        # re-derivation below, so reviewer-side mutation is caught as
        # STALE_REVIEW exactly like check-side mutation.
        reviewer_artifact = _run_reviewer(reviewer, root, audited_id, checks)
    # Re-derive the snapshot from the same policy AFTER the checks: a
    # mismatch means the checks did not consume the audited state.
    current_id = audit_snapshot.snapshot_id_of(root)
    try:
        gate_evidence = _gate_evidence(audited_id, resolved, checks,
                                       reviewer_artifact)
        decision = gate_states.evaluate(
            gate_evidence, authorized_snapshot_id=current_id)
    except gate_states.GateContractError as exc:
        # malformed / incomplete evidence is never authorization
        decision = gate_states.GateDecision(
            gate_states.GateState.INVALID_EVIDENCE, (str(exc),))
    ok = decision.authorized_to_ship
    sealed = evidence.seal({
        "schema": evidence.SCHEMA,
        "stage": "ship",
        "scope": resolved["scope"],
        "commit": evidence.head_hash(root),
        "tree_hash": evidence.tree_hash(root),
        "observed_at": evidence.now_iso(),
        "checks": checks,
        "authorized_to_ship": ok,
        "gate_state": decision.state.value,
        "gate_reasons": list(decision.reasons),
        "blocking_findings": list(decision.blocking_findings),
        "snapshot_id": audited_id,
        "manifest_version": audit_snapshot.MANIFEST_VERSION,
        "policy_version": POLICY_VERSION,
        "semantic_review": reviewer_artifact,
    })
    evidence.write(root, sealed)  # persist BEFORE refusal, as control.py did
    evidence.verify(root)  # roundtrip self-check
    if not ok:
        raise evidence.EvidenceError(
            "SHIP GATE REFUSED: " + decision.state.value
            + ("; " + "; ".join(decision.reasons) if decision.reasons else ""))
    from asha.common import paths as common_paths
    ev_file = common_paths.get_evidence_dir(root) / "evidence.json"
    return json.loads(ev_file.read_text(encoding="utf-8"))


def orient_quick(root: Path) -> dict:
    """Orient the repository (project_map quick mode) — the surface the
    legacy `control.py orient` exposed."""
    root = Path(root).resolve()
    from asha import project_map
    return project_map.build(root, "quick", use_cache=True)


def schema_version() -> int:
    """The evidence schema version the gate seals at."""
    return evidence.SCHEMA