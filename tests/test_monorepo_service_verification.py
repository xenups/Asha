"""Phase 12 (F4): a monorepo verifies every suite its layout declares.

Reproduction (Phase 11, evidence ``rv-20261101T111103Z-52f94cf1``): a commit
adding a genuinely failing test under ``pydetactl/tests`` was authorized with
``COMPLETED_CLEAN`` because ``asha/check_runner.py`` collected ``tests/`` from
the checkout root only and never entered the service directory the changed
file lives in. The evidence read ``"pytest" / "1 passed"``: true of the root
suite, silent about the suite that did not run.

The contract these tests pin:

* the repository suite and every affected service suite run as separate
  checks, in their own working directory and environment;
* the Ship Gate's inventory is the verification PLAN (derived from the
  changed files and the repository layout), never the executor's output, so
  a suite that produced no evidence cannot authorize;
* a service suite that cannot run (missing interpreter/environment, broken
  collection) is a failure, never a skip and never a pass.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from asha import check_runner, evidence, gate_states


def _repo(root: Path, *, service_assertion: str = "assert 2 + 2 == 4",
          root_assertion: str = "assert True",
          extra_service: bool = False) -> Path:
    """A two-level repository: root ``tests/`` plus ``svc/tests``."""
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_root.py").write_text(
        "def test_root():\n    " + root_assertion + "\n", encoding="utf-8")
    (root / "svc" / "src").mkdir(parents=True)
    (root / "svc" / "src" / "mod.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "svc" / "tests").mkdir(parents=True)
    (root / "svc" / "tests" / "test_mod.py").write_text(
        "def test_service():\n    " + service_assertion + "\n",
        encoding="utf-8")
    if extra_service:
        (root / "other" / "tests").mkdir(parents=True)
        (root / "other" / "src").mkdir(parents=True)
        (root / "other" / "src" / "o.py").write_text(
            "x = 1\n", encoding="utf-8")
        (root / "other" / "tests" / "test_o.py").write_text(
            "def test_other():\n    assert True\n", encoding="utf-8")
    return root


def _resolved(*files: str) -> dict:
    return {"checks": ["pytest"], "affected_files": list(files)}


def _entries(root: Path, resolved: dict) -> dict[str, dict]:
    return {e["name"]: e
            for e in check_runner.run(root, resolved, timeout_s=300)}


def _decision(root: Path, resolved: dict, checks: list[dict]):
    """evaluate() on the contract the gate derives from the plan."""
    contract = gate_states.GateEvidence(
        snapshot_id="a" * 64,
        evidence_schema=evidence.SCHEMA,
        policy_version="phase1",
        required_checks=tuple(
            check_runner.required_check_names(root, resolved)),
        checks=tuple(
            gate_states.CheckResult(name=c["name"], status=c["status"],
                                    note=c.get("note") or "",
                                    exit_code=c.get("exit_code"))
            for c in checks),
    )
    return gate_states.evaluate(contract, authorized_snapshot_id="a" * 64)


# --------------------------------------------------------------- Test A
def test_a_service_suite_runs_in_its_own_directory(tmp_path: Path) -> None:
    root = _repo(tmp_path, service_assertion="assert 2 + 2 == 5")
    entries = _entries(root, _resolved("svc/src/mod.py"))
    assert "pytest" in entries and "pytest:svc" in entries
    assert entries["pytest"]["status"] == "passed"
    assert entries["pytest:svc"]["status"] == "failed"
    assert "svc/tests/" in entries["pytest:svc"]["output_tail"]


# --------------------------------------------------------------- Test I
def test_i_real_service_failure_is_not_authorized(tmp_path: Path) -> None:
    root = _repo(tmp_path, service_assertion="assert 2 + 2 == 5")
    resolved = _resolved("svc/src/mod.py")
    decision = _decision(root, resolved,
                         check_runner.run(root, resolved, timeout_s=300))
    assert decision.authorized_to_ship is False
    assert decision.state is not gate_states.GateState.COMPLETED_CLEAN


# --------------------------------------------------------------- Test B
def test_b_healthy_monorepo_authorizes_with_separate_suites(
        tmp_path: Path) -> None:
    root = _repo(tmp_path)
    resolved = _resolved("svc/src/mod.py")
    checks = check_runner.run(root, resolved, timeout_s=300)
    assert check_runner.required_check_names(root, resolved) == [
        "pytest", "pytest:svc"]
    decision = _decision(root, resolved, checks)
    assert decision.state is gate_states.GateState.COMPLETED_CLEAN
    assert decision.authorized_to_ship is True


# --------------------------------------------------------------- Test C
def test_c_missing_service_suite_evidence_denies(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    resolved = _resolved("svc/src/mod.py")
    checks = [e for e in check_runner.run(root, resolved, timeout_s=300)
              if e["name"] != "pytest:svc"]
    decision = _decision(root, resolved, checks)
    assert decision.state is gate_states.GateState.INVALID_EVIDENCE
    assert decision.authorized_to_ship is False
    assert "pytest:svc" in decision.reasons[0]


def test_c2_evidence_for_an_unplanned_suite_denies(tmp_path: Path) -> None:
    """The other direction: a suite nobody planned cannot buy authorization."""
    root = _repo(tmp_path, extra_service=True)
    resolved = _resolved("svc/src/mod.py")
    checks = check_runner.run(root, resolved, timeout_s=300)
    assert "pytest:other" not in {e["name"] for e in checks}
    forged = checks + [{"name": "pytest:other", "status": "passed",
                        "exit_code": 0}]
    decision = _decision(root, resolved, forged)
    assert decision.state is gate_states.GateState.INVALID_EVIDENCE
    assert decision.authorized_to_ship is False


def test_c3_deleting_the_required_suite_denies(tmp_path: Path) -> None:
    """A candidate cannot drop the suite it was supposed to satisfy."""
    root = _repo(tmp_path)
    resolved = _resolved("svc/src/mod.py", "svc/tests/test_mod.py")
    for path in sorted((root / "svc" / "tests").iterdir()):
        path.unlink()
    (root / "svc" / "tests").rmdir()
    checks = check_runner.run(root, resolved, timeout_s=300)
    named = {e["name"]: e for e in checks}
    assert named["pytest:svc"]["status"] == "failed"
    assert "missing" in named["pytest:svc"]["note"]
    assert _decision(root, resolved, checks).authorized_to_ship is False


# --------------------------------------------------------------- Test D
def test_d_unavailable_service_environment_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path)

    def _raise(*_args, **_kwargs):
        raise check_runner.CheckRunnerError("no interpreter for service svc")

    monkeypatch.setattr(check_runner, "_service_env", _raise)
    resolved = _resolved("svc/src/mod.py")
    checks = check_runner.run(root, resolved, timeout_s=300)
    named = {e["name"]: e for e in checks}
    assert named["pytest:svc"]["status"] == "failed"
    assert "no interpreter" in named["pytest:svc"]["note"]
    assert _decision(root, resolved, checks).authorized_to_ship is False


# --------------------------------------------------------------- Test E
def test_e_service_collection_error_is_a_failure(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "svc" / "tests" / "test_broken.py").write_text(
        "import a_module_that_is_not_installed\n", encoding="utf-8")
    entries = _entries(root, _resolved("svc/src/mod.py"))
    service = entries["pytest:svc"]
    assert service["status"] == "failed"
    assert service["exit_code"] == 2
    assert entries["pytest"]["status"] == "passed"


# --------------------------------------------------------------- Test F
def test_f_unaffected_service_is_not_executed(tmp_path: Path) -> None:
    root = _repo(tmp_path, extra_service=True)
    resolved = _resolved("svc/src/mod.py")
    assert check_runner.required_check_names(root, resolved) == [
        "pytest", "pytest:svc"]
    assert "pytest:other" not in _entries(root, resolved)


# --------------------------------------------------------------- Test G
def test_g_evidence_cannot_be_reused_from_another_checkout(
        tmp_path: Path) -> None:
    """A run from a layout without the service cannot satisfy this plan."""
    here = _repo(tmp_path / "here")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "tests").mkdir(parents=True)
    (elsewhere / "tests" / "test_root.py").write_text(
        "def test_root():\n    assert True\n", encoding="utf-8")
    resolved = _resolved("svc/src/mod.py")
    foreign = check_runner.run(elsewhere, resolved, timeout_s=300)
    decision = _decision(here, resolved, foreign)
    assert decision.state is gate_states.GateState.INVALID_EVIDENCE
    assert decision.authorized_to_ship is False


# --------------------------------------------------------------- Test H
def test_h_root_suite_stays_independent(tmp_path: Path) -> None:
    root = _repo(tmp_path, root_assertion="assert 1 == 2")
    entries = _entries(root, _resolved("svc/src/mod.py"))
    assert entries["pytest"]["status"] == "failed"
    assert entries["pytest:svc"]["status"] == "passed"
    assert entries["pytest"]["scope"] == "package"
    assert entries["pytest:svc"]["scope"] == "package"
    assert entries["pytest"]["service"] == "."
    assert entries["pytest:svc"]["service"] == "svc"


# ------------------------------------------------------ root-only layout
def test_root_only_repository_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir(parents=True)
    (tmp_path / "tests" / "test_root.py").write_text(
        "def test_root():\n    assert True\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    resolved = {"checks": ["pytest"], "affected_files": ["mod.py"]}
    assert check_runner.required_check_names(tmp_path, resolved) == ["pytest"]
    assert list(_entries(tmp_path, resolved)) == ["pytest"]
