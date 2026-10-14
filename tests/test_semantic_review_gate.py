"""Phase 2 (P2.3) -- semantic reviewer gate: adversarial verification matrix.

Six mandated categories, each pinned to the boundary it proves:

  A. schema determinism     empty/invalid fields and severities are
                            rejected, never normalized
  B. severity partition     BLOCKER/HIGH deny; MEDIUM/LOW allow (with
                            the rule in gate_reasons); zero findings is
                            COMPLETED_CLEAN
  C. snapshot binding       wrong/missing artifact snapshot_id ->
                            INVALID_EVIDENCE -> deny
  D. failure resilience     reviewer timeout, crash and non-object
                            output -> EXECUTION_FAILED -> deny; missing
                            reviewer and malformed findings ->
                            INVALID_EVIDENCE -> deny
  E. backward compatibility phase1 policy and legacy behaviors unchanged
  F. AST guard              no literal `authorized_to_ship = True`
                            bypass path exists anywhere in asha/

Boundary tests drive ship_gate.gate_ship against a real scratch git
repository (same harness discipline as test_audit_snapshot.py); pure
contract cases drive gate_states/semantic_review directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from asha import evidence, gate_states
from asha.common import paths as common_paths
from asha.governance import semantic_review, ship_gate

GITIGNORE = (
    ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
    ".mypy_cache/\n.ruff_cache/\nvenv/\n"
)

BASELINE = {
    ".gitignore": GITIGNORE,
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    "pkg/leaf.py": "VALUE = 1\n",
}


# --------------------------------------------------------------- fixtures

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True,
                   text=True, timeout=60, check=True)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in BASELINE.items():
        _write(repo, rel, text)
    _commit_all(repo, "baseline")
    return repo


def _sealed(repo: Path) -> dict:
    path = common_paths.get_evidence_dir(repo) / "evidence.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _finding(severity: str = "MEDIUM", **overrides: str) -> dict:
    """A schema-valid reviewer finding; override individual fields."""
    entry = {
        "severity": severity,
        "file_path": "pkg/leaf.py",
        "line_range": "1-2",
        "problem": "test problem",
        "why_it_matters": "test consequence",
        "failure_scenario": "test scenario",
        "recommended_fix": "test fix",
    }
    entry.update(overrides)
    return entry


def _artifact(snapshot_id: str, findings: tuple[dict, ...] = (),
              **overrides: object) -> dict:
    """A valid semantic-review/v1 artifact bound to snapshot_id."""
    artifact = {
        "schema": semantic_review.REVIEW_SCHEMA,
        "snapshot_id": snapshot_id,
        "policy_version": ship_gate.POLICY_VERSION,
        "status": "ok",
        "findings": list(findings),
    }
    artifact.update(overrides)
    return artifact


def _evidence_payload(findings: tuple[dict, ...] = (), *,
                      policy: str = "phase2",
                      artifact: dict | None | object = "default",
                      snapshot_id: str = "a" * 64) -> dict:
    """Raw evidence dict at the dict-entry boundary of validate_evidence."""
    if artifact == "default":
        artifact = (_artifact(snapshot_id, findings)
                    if policy == "phase2" else None)
    payload = {
        "snapshot_id": snapshot_id,
        "evidence_schema": evidence.SCHEMA,
        "policy_version": policy,
        "required_checks": ["ruff", "pytest", "mypy"],
        "checks": [
            {"name": name, "status": "passed", "exit_code": 0}
            for name in ("ruff", "pytest", "mypy")
        ],
        "findings": list(findings),
    }
    if artifact is not None:
        payload["semantic_review"] = artifact
    return payload


def _evaluate(findings: tuple[dict, ...] = (), **kwargs: object
              ) -> gate_states.GateDecision:
    """Validate + evaluate in one step (contract-level cases)."""
    return gate_states.evaluate(
        gate_states.validate_evidence(_evidence_payload(findings, **kwargs)),
        authorized_snapshot_id="a" * 64)


def _clean_reviewer(root: Path, snapshot_id: str, timeout_s: int) -> dict:
    return _artifact(snapshot_id)


def _reviewer_returning(artifact: object):
    def _fn(root: Path, snapshot_id: str, timeout_s: int) -> object:
        return artifact
    return _fn


def _reviewer_raising(exc: Exception):
    def _fn(root: Path, snapshot_id: str, timeout_s: int) -> dict:
        raise exc
    return _fn


# ------------------------------------------- A. schema determinism

def test_finding_rejects_empty_fields() -> None:
    """Every non-severity field must be a non-empty string; '' and ' '
    must raise at construction, never normalize into a valid finding."""
    for field in semantic_review.FINDING_FIELDS[1:]:
        for bad in ("", "   "):
            with pytest.raises(gate_states.GateContractError):
                semantic_review.Finding(**_finding(**{field: bad}))


def test_finding_rejects_invalid_severities() -> None:
    """Only BLOCKER/HIGH/MEDIUM/LOW; unknown values (and non-strings)
    fail closed."""
    for bad in ("CRITICAL", "blocker", "", "Medium", 7, None, True):
        with pytest.raises(gate_states.GateContractError):
            semantic_review.Finding(**_finding(severity=bad))


def test_finding_rejects_non_string_field_types() -> None:
    """Non-string values for text fields are rejected, not coerced."""
    for field in ("line_range", "problem", "recommended_fix"):
        with pytest.raises(gate_states.GateContractError):
            semantic_review.Finding(**{**_finding(), field: 7})


def test_validate_finding_shapes() -> None:
    """Non-dict entries and missing keys are rejected; a valid entry is
    returned unchanged (deterministic pass-through)."""
    for bad in (None, "finding", 7, ["severity"]):
        with pytest.raises(gate_states.GateContractError):
            semantic_review.validate_finding(bad)
    partial = {key: value for key, value in _finding().items()
               if key != "why_it_matters"}
    with pytest.raises(gate_states.GateContractError):
        semantic_review.validate_finding(partial)
    valid = _finding()
    assert semantic_review.validate_finding(valid) is valid


def test_severity_constants_are_exact() -> None:
    assert semantic_review.SEVERITY_VALUES == frozenset(
        {"BLOCKER", "HIGH", "MEDIUM", "LOW"})
    assert semantic_review.BLOCKING_SEVERITY_STRINGS == frozenset(
        {"BLOCKER", "HIGH"})
    assert semantic_review.SEVERITY_BLOCKS_SHIP == frozenset(
        {semantic_review.Severity.BLOCKER, semantic_review.Severity.HIGH})
    assert semantic_review.REVIEW_SCHEMA == "semantic-review/v1"


# ------------------------------------------- B. severity partition

@pytest.mark.parametrize("severity", ["BLOCKER", "HIGH"])
def test_blocking_severity_denies(severity: str) -> None:
    decision = _evaluate((_finding(severity),))
    assert decision.state is gate_states.GateState.COMPLETED_WITH_FINDINGS
    assert decision.authorized_to_ship is False
    assert decision.blocking_findings
    assert severity in decision.reasons[0]


@pytest.mark.parametrize("severity", ["MEDIUM", "LOW"])
def test_non_blocking_severity_allows(severity: str) -> None:
    decision = _evaluate((_finding(severity),))
    assert decision.state is gate_states.GateState.COMPLETED_WITH_FINDINGS
    assert decision.authorized_to_ship is True
    assert not decision.blocking_findings
    # the rule must be documented in gate_reasons (approved policy)
    assert "SEVERITY_BLOCKS_SHIP" in decision.reasons[0]


def test_zero_findings_is_completed_clean() -> None:
    decision = _evaluate()
    assert decision.state is gate_states.GateState.COMPLETED_CLEAN
    assert decision.authorized_to_ship is True


def test_mixed_inventory_denies_on_its_blocking_member() -> None:
    decision = _evaluate((_finding("LOW"), _finding("HIGH")))
    assert decision.authorized_to_ship is False
    assert decision.blocking_findings


# ------------------------------------------- C. snapshot binding

def test_artifact_bound_to_wrong_snapshot_is_invalid() -> None:
    payload = _evidence_payload(artifact=_artifact("b" * 64))
    with pytest.raises(gate_states.GateContractError):
        gate_states.validate_evidence(payload)


def test_artifact_without_snapshot_id_is_invalid() -> None:
    artifact = _artifact("a" * 64)
    del artifact["snapshot_id"]
    with pytest.raises(gate_states.GateContractError):
        gate_states.validate_evidence(
            _evidence_payload(artifact=artifact))


def test_missing_artifact_under_phase2_is_invalid() -> None:
    with pytest.raises(gate_states.GateContractError):
        gate_states.validate_evidence(_evidence_payload(artifact=None))


def test_unknown_artifact_schema_is_invalid() -> None:
    with pytest.raises(gate_states.GateContractError):
        gate_states.validate_evidence(
            _evidence_payload(artifact=_artifact("a" * 64,
                                                 schema="semantic-review/v0")))


def test_artifact_policy_version_mismatch_is_invalid(
        tmp_path: Path) -> None:
    """C6: the artifact's stamped policy_version must equal the policy
    the gate enforces; a foreign-policy artifact is INVALID_EVIDENCE,
    never a silently accepted review (audit observation on
    semantic_review.validate_artifact)."""
    # contract boundary: 'phase1' stamp under phase2 evidence
    with pytest.raises(gate_states.GateContractError):
        gate_states.validate_evidence(
            _evidence_payload(artifact=_artifact("a" * 64,
                                                 policy_version="phase1")))
    # direct contract call carries the expected policy explicitly
    with pytest.raises(gate_states.GateContractError):
        semantic_review.validate_artifact(
            _artifact("a" * 64, policy_version="phase1"),
            snapshot_id="a" * 64, policy_version="phase2")
    # end-to-end: a reviewer that stamps a foreign policy version
    repo = _make_repo(tmp_path)

    def _reviewer(root: Path, snapshot_id: str, timeout_s: int) -> dict:
        return _artifact(snapshot_id, policy_version="phase1")
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo, reviewer=_reviewer)
    assert "INVALID_EVIDENCE" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


def test_malformed_finding_in_payload_is_invalid() -> None:
    payload = _evidence_payload((_finding(problem=""),))
    with pytest.raises(gate_states.GateContractError):
        gate_states.validate_evidence(payload)


def test_evaluation_requires_fresh_target_snapshot() -> None:
    """The authorization target must match the audited snapshot id."""
    valid = gate_states.validate_evidence(_evidence_payload())
    assert gate_states.evaluate(
        valid, authorized_snapshot_id="b" * 64
    ).state is gate_states.GateState.STALE_REVIEW


# ------------------------------------- D. failure resilience (boundary)

def test_reviewer_timeout_maps_to_execution_failed(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(
            repo, reviewer=_reviewer_raising(TimeoutError("boom")))
    assert "EXECUTION_FAILED" in str(exc.value)
    sealed = _sealed(repo)
    assert sealed["authorized_to_ship"] is False
    review_checks = [check for check in sealed["checks"]
                     if check["name"] == "semantic-review"]
    assert review_checks and review_checks[0]["status"] == "failed"
    assert "timed out" in review_checks[0]["note"]


def test_reviewer_crash_maps_to_execution_failed(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(
            repo, reviewer=_reviewer_raising(RuntimeError("boom")))
    assert "EXECUTION_FAILED" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


def test_reviewer_non_object_output_maps_to_execution_failed(
        tmp_path: Path) -> None:
    """Malformed (non-object) reviewer output fails as an execution
    failure, never as a silent clean review."""
    repo = _make_repo(tmp_path)
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(
            repo, reviewer=_reviewer_returning("{not-json"))
    assert "EXECUTION_FAILED" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


def test_missing_reviewer_fails_closed_invalid_evidence(
        tmp_path: Path) -> None:
    """phase2 requires a review: no executor -> INVALID_EVIDENCE."""
    repo = _make_repo(tmp_path)
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo)
    assert "INVALID_EVIDENCE" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


def test_wrong_snapshot_review_is_invalid_evidence(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    reviewer = _reviewer_returning(_artifact("b" * 64))
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo, reviewer=reviewer)
    assert "INVALID_EVIDENCE" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


def test_malformed_finding_from_reviewer_is_invalid_evidence(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    def _reviewer(root: Path, snapshot_id: str, timeout_s: int) -> dict:
        return _artifact(snapshot_id,
                         (_finding(problem=""),))
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo, reviewer=_reviewer)
    assert "INVALID_EVIDENCE" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


def test_blocker_finding_denies_end_to_end(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    def _reviewer(root: Path, snapshot_id: str, timeout_s: int) -> dict:
        return _artifact(snapshot_id, (_finding("BLOCKER"),))
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo, reviewer=_reviewer)
    assert "COMPLETED_WITH_FINDINGS" in str(exc.value)
    sealed = _sealed(repo)
    assert sealed["authorized_to_ship"] is False
    assert sealed["gate_state"] == "COMPLETED_WITH_FINDINGS"
    assert sealed["blocking_findings"] == ["pkg/leaf.py:BLOCKER"]
    assert any("BLOCKER" in reason for reason in sealed["gate_reasons"])
    # the artifact is sealed with the evidence (tamper-evident)
    assert sealed["semantic_review"]["findings"][0]["severity"] == "BLOCKER"


def test_medium_finding_authorizes_end_to_end(tmp_path: Path) -> None:
    """Approved Phase 2 rule: a valid, non-blocking finding authorizes
    with COMPLETED_WITH_FINDINGS recorded and the rule documented."""
    repo = _make_repo(tmp_path)

    def _reviewer(root: Path, snapshot_id: str, timeout_s: int) -> dict:
        return _artifact(snapshot_id, (_finding("MEDIUM"),
                                       _finding("LOW")))
    payload = ship_gate.gate_ship(repo, reviewer=_reviewer)
    assert payload["authorized_to_ship"] is True
    assert payload["gate_state"] == "COMPLETED_WITH_FINDINGS"
    assert any("SEVERITY_BLOCKS_SHIP" in reason
               for reason in payload["gate_reasons"])
    assert payload["policy_version"] == "phase2"
    assert len(payload["semantic_review"]["findings"]) == 2
    # digest covers the findings-bearing artifact
    assert evidence.compute_digest(payload) == payload["evidence_sha256"]


def test_clean_review_authorizes_end_to_end(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payload = ship_gate.gate_ship(repo, reviewer=_clean_reviewer)
    assert payload["authorized_to_ship"] is True
    assert payload["gate_state"] == "COMPLETED_CLEAN"


# --------------------------------------- E. backward compatibility

def test_phase1_policy_still_denies_schema_less_findings() -> None:
    """Legacy policy: findings never authorize, no artifact required,
    no severity schema enforced (behavior pinned before Phase 2)."""
    decision = _evaluate(({"rule": "R1", "path": "pkg/leaf.py"},),
                         policy="phase1", artifact=None)
    assert decision.state is gate_states.GateState.COMPLETED_WITH_FINDINGS
    assert decision.authorized_to_ship is False


def test_state_allows_ship_unchanged() -> None:
    assert gate_states.STATE_ALLOWS_SHIP == frozenset(
        {gate_states.GateState.COMPLETED_CLEAN})


def test_no_execute_still_invalid(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo, no_execute=True,
                            reviewer=_clean_reviewer)
    assert "INVALID_EVIDENCE" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


# ------------------------------------------- F. AST guard

def test_no_literal_true_authorization_path() -> None:
    """No module may hard-code an authorizing literal True; the gate
    derives the boolean. AST walk (not regex) with a positive probe."""
    import ast
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "asha").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Name)
                            and target.id == "authorized_to_ship"
                            and isinstance(node.value, ast.Constant)
                            and node.value.value is True):
                        offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant)
                            and key.value == "authorized_to_ship"
                            and isinstance(value, ast.Constant)
                            and value.value is True):
                        offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"hard-coded authorization literal(s): {offenders}"
    probe = ast.parse("payload = {'authorized_to_ship': True}\n")
    hits = [node for node in ast.walk(probe)
            if isinstance(node, ast.Dict)
            and any(isinstance(key, ast.Constant)
                    and key.value == "authorized_to_ship"
                    for key in node.keys)]
    assert hits, "the AST probe must detect the banned shape"
