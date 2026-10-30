"""Adversarial regression tests for the Phase 12 evidence contract.

Every test drives the real handlers over a throwaway git repository and
asserts BOTH the serialized artifact and the resulting governance decision,
so a pass here means the decision path (not just a helper) behaves.

Contract under test (asha/validation.py):

    execution_status   did the command run, and how did it end?
    validation_status  did the required policy run, and did it pass?
    ready_to_push      execution + validation + clean tree + sealed evidence

Invariants: a zero exit code is not a validation verdict; an empty check
inventory is NOT_CONFIGURED; skipped/blocked/unlaunchable checks never pass;
a synthetic or legacy artifact never counts as validation evidence.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from asha import check_runner, evidence, validation
from asha import mcp_server as server
from asha.common import paths as common_paths

PY = sys.executable
EMPTY_DIGEST = validation.digest_text("")


# ------------------------------------------------------------------ helpers

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _write(repo / "pkg/base.py", "VALUE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _dispatch(repo: Path, **kwargs: object) -> dict:
    """Dispatch with an explicit committed scope (hermetic, clean tree)."""
    args: dict = {"root": str(repo), "id": "t",
                  "declared_scope": ["pkg/base.py"],
                  "execution_mode": "in_place_atomic"}
    args.update(kwargs)
    return server.asha_dispatch_task(args)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _tip_artifact(repo: Path, head: str, tree: str | None = None) -> Path:
    tree = tree or _git(repo, "rev-parse", "HEAD^{tree}").strip()
    return (common_paths.get_verify_tip_dir(repo)
            / f"{head[:12]}-{tree[:12]}.evidence.json")


def _sealed(repo: Path, head: str) -> dict:
    return json.loads(_tip_artifact(repo, head).read_text(encoding="utf-8"))


def _seal_artifact(repo: Path, head: str, **overrides: object) -> Path:
    payload: dict = {
        "schema": evidence.SCHEMA_VALIDATED,
        "stage": "verify_tip",
        "kind": "verification",
        "scope": "S2",
        "commit": head,
        "tree_hash": _git(repo, "rev-parse", "HEAD^{tree}").strip(),
        "snapshot_id": "0" * 64,
        "observed_at": evidence.now_iso(),
        "execution": {"argv": ["true"], "exit_code": 0,
                      "termination": "exited", "duration_ms": 1,
                      "started_at": evidence.now_iso(),
                      "stdout_sha256": EMPTY_DIGEST,
                      "stderr_sha256": EMPTY_DIGEST,
                      "stdout_tail": "", "stderr_tail": "",
                      "known_policy_command": False, "cwd": "."},
        "checks": [{"name": "validation[0]", "status": "passed",
                    "launched": True, "exit_code": 0}],
        "validation": {"status": "PASS", "policy": "explicit",
                       "rule": "all required checks passed"},
        "validation_status": "PASS",
        "ready_to_push": True,
        "reason": None,
        "authorized_to_ship": False,
    }
    payload.update(overrides)
    sealed = evidence.seal(payload)
    path = _tip_artifact(repo, head, str(payload["tree_hash"]))
    evidence.write_to(path, sealed)
    return path


# ------------------------------------------------- 1. execution vs verdict

def test_zero_exit_without_validation_is_not_a_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = _dispatch(repo, cmd=[PY, "-c", "print('ok')"])
    payload = _payload(result)
    assert payload["state"] == "DONE"
    assert payload["execution"]["exit_code"] == 0
    assert payload["execution"]["termination"] == "exited"
    assert payload["execution"]["duration_ms"] >= 0
    assert payload["execution"]["stdout_sha256"] != EMPTY_DIGEST
    # measured, not hardcoded: the dispatch timing mirrors the record
    assert payload["timings"]["execution_ms"] ==         payload["execution"]["duration_ms"]
    assert isinstance(payload["timings"]["evidence_ms"], int)
    assert payload["timings"]["evidence_ms"] >= 0
    assert payload["validation_status"] == validation.VALIDATION_NOT_CONFIGURED
    assert payload["validation_result"] == "FAIL"
    assert payload["ready_to_push"] is False
    assert payload["reason"] == "VALIDATION_NOT_CONFIGURED"
    assert payload["authorized_to_ship"] is False

    dispatch_artifact = json.loads(
        Path(payload["evidence"]["path"]).read_text(encoding="utf-8"))
    assert dispatch_artifact["kind"] == "execution_attestation"

    sealed = _sealed(repo, payload["head"])
    assert sealed["checks"] == []
    assert sealed["ok"] is False
    assert sealed["ready_to_push"] is False
    assert sealed["kind"] == "execution_attestation"
    assert sealed["execution"]["exit_code"] == 0


def test_zero_exit_with_passing_validation_is_ready(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = _dispatch(repo, cmd=[PY, "-c", "pass"],
                       validation=[PY + " -c pass"])
    payload = _payload(result)
    assert payload["validation_status"] == validation.VALIDATION_PASS
    assert payload["validation_result"] == "PASS"
    assert payload["ready_to_push"] is True
    assert payload.get("reason") is None

    assert [c["name"] for c in payload["checks"]] == ["validation[0]"]
    dispatch_artifact = json.loads(
        Path(payload["evidence"]["path"]).read_text(encoding="utf-8"))
    assert dispatch_artifact["kind"] == "verification"

    sealed = _sealed(repo, payload["head"])
    assert sealed["kind"] == "verification"
    assert sealed["checks"][0]["exit_code"] == 0
    assert sealed["checks"][0]["launched"] is True
    assert sealed["validation"]["policy"] == "explicit"
    assert sealed["execution"]["argv"][-1] == "pass"
    assert sealed["authorized_to_ship"] is False


def test_nonzero_exit_is_recorded_and_not_ready(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = _dispatch(repo, cmd=[PY, "-c", "raise SystemExit(3)"],
                       validation=[PY + " -c pass"])
    payload = _payload(result)
    assert payload["state"] == "FAILED"
    assert payload["execution"]["exit_code"] == 3
    assert payload["ready_to_push"] is False
    assert payload["evidence"] == {}


def test_command_launch_failure_is_not_a_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = _dispatch(repo, cmd=["asha-no-such-binary-xyz"])
    payload = _payload(result)
    assert payload["state"] == "FAILED"
    assert payload["execution"]["termination"] == "launch_failed"
    assert payload["execution"]["exit_code"] is None
    assert payload["ready_to_push"] is False


def test_validation_launch_failure_is_blocked(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = _dispatch(repo, cmd=[PY, "-c", "pass"],
                       validation=["asha-no-such-binary-xyz"])
    payload = _payload(result)
    assert payload["validation_status"] == validation.VALIDATION_BLOCKED
    assert payload["reason"] == "VALIDATION_BLOCKED"
    assert payload["ready_to_push"] is False


def test_required_validation_failure_is_fail(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = _dispatch(repo, cmd=[PY, "-c", "pass"],
                       validation=[PY + " -c 'raise SystemExit(7)'"])
    payload = _payload(result)
    assert payload["validation_status"] == validation.VALIDATION_FAIL
    assert payload["validation_result"] == "FAIL"
    assert payload["ready_to_push"] is False


# ------------------------------------------- 2. policy vocabulary is total

def test_empty_inventory_is_not_configured() -> None:
    status, rule = validation.validation_status([])
    assert status == validation.VALIDATION_NOT_CONFIGURED
    assert "no required checks" in rule


def test_skipped_check_is_not_a_pass() -> None:
    status, rule = validation.validation_status(
        [{"name": "pytest", "status": "skipped", "exit_code": 0}])
    assert status == validation.VALIDATION_BLOCKED
    assert "not executed" in rule


def test_blocked_and_failed_entries_are_not_passed() -> None:
    blocked, _ = validation.validation_status(
        [{"name": "ruff", "status": "failed", "launched": False,
          "exit_code": None}])
    assert blocked == validation.VALIDATION_BLOCKED
    failed, _ = validation.validation_status(
        [{"name": "ruff", "status": "failed", "launched": True,
          "exit_code": 1}])
    assert failed == validation.VALIDATION_FAIL


def test_push_readiness_requires_every_input() -> None:
    ok_exec = {"termination": "exited", "exit_code": 0}
    assert validation.push_readiness(
        execution=ok_exec, validation=validation.VALIDATION_PASS,
        clean_tree=True, evidence_verified=True) == (True, "READY")
    assert validation.push_readiness(
        execution=ok_exec, validation=validation.VALIDATION_NOT_CONFIGURED,
        clean_tree=True, evidence_verified=True)[1] == \
        "VALIDATION_NOT_CONFIGURED"
    assert validation.push_readiness(
        execution=ok_exec, validation=validation.VALIDATION_PASS,
        clean_tree=False, evidence_verified=True)[1] == "WIP_UNCOMMITTED_PASS"
    assert validation.push_readiness(
        execution=ok_exec, validation=validation.VALIDATION_PASS,
        clean_tree=True, evidence_verified=False)[1] == "EVIDENCE_UNVERIFIED"


# --------------------------------------- 3. artifact-level fail-closed rules

def test_synthetic_attestation_is_not_validation_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    path = _seal_artifact(repo, head, kind="execution_attestation",
                          checks=[], validation_status="NOT_CONFIGURED",
                          ready_to_push=True)
    sealed = json.loads(path.read_text(encoding="utf-8"))
    assert server._artifact_evidence_state(sealed, head, tree) == "unvalidated"


def test_legacy_schema_is_not_reinterpreted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    path = _seal_artifact(repo, head, schema=evidence.SCHEMA)
    sealed = json.loads(path.read_text(encoding="utf-8"))
    assert server._artifact_evidence_state(sealed, head, tree) == "legacy"


def test_tampered_digest_is_detected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    path = _seal_artifact(repo, head)
    sealed = json.loads(path.read_text(encoding="utf-8"))
    assert server._artifact_evidence_state(sealed, head, tree) == "verified"
    sealed["checks"][0]["status"] = "failed"
    assert server._artifact_evidence_state(sealed, head, tree) == "tampered"


def test_evidence_for_another_commit_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    _seal_artifact(repo, head, commit="0" * 40)
    sealed = _sealed(repo, head)
    assert server._artifact_evidence_state(sealed, head, tree) == "mismatch"


def test_ready_false_artifact_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    path = _seal_artifact(repo, head, ready_to_push=False)
    sealed = json.loads(path.read_text(encoding="utf-8"))
    assert server._artifact_evidence_state(sealed, head, tree) == "unready"


def test_stack_refuses_attestation_only_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "b1")
    _write(repo / "pkg/b1.py", "ONE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b1")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _seal_artifact(repo, head, kind="execution_attestation", checks=[],
                   validation_status="NOT_CONFIGURED", ready_to_push=True)

    result = server.asha_verify_stack({"root": str(repo),
                                       "branches": ["b1"],
                                       "base_ref": "main"})
    payload = _payload(result)
    assert payload["stack_healthy"] is False
    row = payload["branches"][0]
    assert row["evidence"] == "unvalidated"
    assert row["status"] == "STACK_EVIDENCE_UNVALIDATED"
    assert row["evidence_verified"] is False


def test_stack_accepts_verified_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "b1")
    _write(repo / "pkg/b1.py", "ONE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b1")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _seal_artifact(repo, head)
    result = server.asha_verify_stack({"root": str(repo),
                                       "branches": ["b1"],
                                       "base_ref": "main"})
    payload = _payload(result)
    assert payload["stack_healthy"] is True, payload
    assert payload["branches"][0]["evidence"] == "verified"


# ------------------------------------------------- 4. secrecy and isolation

def test_validation_arguments_and_output_are_redacted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASHA_TEST_TOKEN", "s3cr3t-value")
    repo = _repo(tmp_path)
    result = _dispatch(
        repo, cmd=[PY, "-c", "pass"],
        validation=[PY + " -c \"print('ASHA_TEST_TOKEN=s3cr3t-value')\""])
    payload = _payload(result)
    sealed = _sealed(repo, payload["head"])
    blob = json.dumps(sealed)
    assert "s3cr3t-value" not in blob
    assert "[REDACTED]" in sealed["execution"]["stdout_tail"] or \
        "[REDACTED]" in sealed["checks"][0]["output_tail"]


def test_cross_service_scope_is_grouped_not_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for name in ("svc_a", "svc_b"):
        _write(repo / name / "pyproject.toml",
               "[project]\nname = \"" + name + "\"\nversion = \"0.0.0\"\n")
        _write(repo / name / "app.py", "VALUE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two services")

    groups = check_runner._service_groups(
        repo, ["svc_a/app.py", "svc_b/app.py"])
    assert [label for label, _ in groups] == ["svc_a", "svc_b"]

    results = check_runner.run(repo, {
        "checks": ["ruff"],
        "affected_files": ["svc_a/app.py", "svc_b/app.py"]})
    assert {entry["service"] for entry in results} == {"svc_a", "svc_b"}
    status, _rule = validation.validation_status(results)
    assert status in (validation.VALIDATION_PASS, validation.VALIDATION_FAIL,
                      validation.VALIDATION_BLOCKED)


def test_cross_service_blocked_group_never_passes(tmp_path: Path) -> None:
    """One service whose interpreter cannot run blocks the aggregate; the
    other service's verdict is still recorded (no silent omission)."""
    repo = _repo(tmp_path)
    for name in ("svc_a", "svc_b"):
        _write(repo / name / "pyproject.toml",
               "[project]\nname = \"" + name + "\"\nversion = \"0.0.0\"\n")
        _write(repo / name / "app.py", "VALUE = 1\n")
    broken = repo / "svc_b" / ".venv" / "bin" / "python"
    broken.parent.mkdir(parents=True)
    broken.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    broken.chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two services")

    results = check_runner.run(repo, {
        "checks": ["ruff"],
        "affected_files": ["svc_a/app.py", "svc_b/app.py"]})
    assert {entry["service"] for entry in results} == {"svc_a", "svc_b"}
    assert any(entry["service"] == "svc_b" and entry["status"] != "passed"
               for entry in results)
    status, _rule = validation.validation_status(results)
    # a broken service interpreter is never a pass: it may launch and exit
    # non-zero (FAIL) or never launch (BLOCKED); both deny readiness
    assert status in (validation.VALIDATION_FAIL, validation.VALIDATION_BLOCKED)


def test_validation_timeout_is_blocked_not_passed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    entries = validation.run_validation_commands(
        [[PY, "-c", "import time; time.sleep(5)"]], repo, timeout_s=1)
    assert entries[0]["status"] == "failed"
    assert entries[0]["launched"] is False
    assert validation.validation_status(entries)[0] == \
        validation.VALIDATION_BLOCKED


def test_seal_without_snapshot_identity_is_tampered(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    path = _seal_artifact(repo, head, snapshot_id=None)
    sealed = json.loads(path.read_text(encoding="utf-8"))
    assert server._artifact_evidence_state(sealed, head, tree) == "tampered"


def test_seal_with_malformed_snapshot_identity_is_tampered(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    path = _seal_artifact(repo, head, snapshot_id="short")
    sealed = json.loads(path.read_text(encoding="utf-8"))
    assert server._artifact_evidence_state(sealed, head, tree) == "tampered"
def test_ship_authority_claim_is_tampering(tmp_path: Path) -> None:
    """A recomputed digest cannot make a ship-authority claim trustworthy."""
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    path = _seal_artifact(repo, head, authorized_to_ship=True)
    sealed = json.loads(path.read_text(encoding="utf-8"))
    # the digest is internally consistent; only the semantic check can catch it
    assert sealed["evidence_sha256"] == evidence.compute_digest(sealed)
    assert server._artifact_evidence_state(sealed, head, tree) == "tampered"


def test_stack_rejects_ship_authority_claim(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "b1")
    _write(repo / "pkg/b1.py", "ONE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b1")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _seal_artifact(repo, head, authorized_to_ship=True)

    payload = _payload(server.asha_verify_stack(
        {"root": str(repo), "branches": ["b1"], "base_ref": "main"}))
    assert payload["stack_healthy"] is False
    assert payload["branches"][0]["evidence"] == "tampered"
    assert payload["branches"][0]["status"] == "STACK_EVIDENCE_TAMPERED"
