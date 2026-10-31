"""Phase 5: a gate refusal must be recorded as a refusal, never as a pass.

The defect this pins down: ``gate_ship`` refuses BEFORE sealing when the
worktree is dirty, and ``orchestrator._run_gate`` then read the artifact back
from disk -- which is the PREVIOUS run's sealed decision. A repository that had
ever passed a clean gate therefore reported ``gate_state=COMPLETED_CLEAN`` with
``authorized_to_ship=false`` for a run whose checks never started.

Every test here drives the real gate on a real repository; nothing stubs
``gate_ship``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from asha import gate_states
from asha.common import paths as common_paths
from asha.governance import semantic_review, ship_gate
from asha.ladder import orchestrator

BASELINE = {
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "pkg/__init__.py": "",
    "pkg/leaf.py": "VALUE = 1\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "fixture@example.com"),
                 ("config", "user.name", "Fixture"),
                 ("config", "commit.gpgsign", "false")):
        _git(repo, *args)
    for rel, text in BASELINE.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _reviewer(root: Path, snapshot_id: str, timeout_s: int) -> dict:
    return {
        "schema": semantic_review.REVIEW_SCHEMA,
        "snapshot_id": snapshot_id,
        "policy_version": ship_gate.POLICY_VERSION,
        "status": "ok",
        "findings": [],
    }


def _sealed(root: Path) -> dict:
    path = common_paths.get_evidence_dir(root) / "evidence.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _checks(root: Path, *entries: dict) -> list[dict]:
    sealed = _sealed(root)
    return [entry for entry in sealed["checks"]
            if entry["name"] in {item["name"] for item in entries}]


def _refusal(repo: Path) -> dict:
    with pytest.raises(ship_gate.GateRefused) as exc:
        ship_gate.gate_ship(repo, reviewer=_reviewer)
    return exc.value.gate


# ------------------------------------------------------------ refusal paths

def test_dirty_tracked_file_refuses_without_running_checks(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "pkg/leaf.py").write_text("VALUE = 2\n", encoding="utf-8")

    gate = _refusal(repo)

    assert gate["gate_state"] == gate_states.GateState.REFUSED.value
    assert gate["authorized_to_ship"] is False
    assert any("working tree is dirty" in reason for reason in gate["reasons"])
    assert not (common_paths.get_evidence_dir(repo) / "evidence.json").exists()


def test_new_untracked_file_refuses_without_running_checks(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "extra_module.py").write_text("X = 1\n", encoding="utf-8")

    gate = _refusal(repo)

    assert gate["gate_state"] == gate_states.GateState.REFUSED.value
    assert gate["authorized_to_ship"] is False
    assert any("extra_module.py" in reason for reason in gate["reasons"])


def test_a_previous_clean_artifact_cannot_relabel_a_refusal(
        tmp_path: Path) -> None:
    """The exact defect: a clean gate first, then a refusal on the same repo.

    Reading the outcome back from disk reported the first run's
    ``COMPLETED_CLEAN`` for the second, whose checks never started.
    """
    repo = _make_repo(tmp_path)
    first = ship_gate.gate_ship(repo, reviewer=_reviewer)
    assert first["gate_state"] == "COMPLETED_CLEAN"
    before = (common_paths.get_evidence_dir(repo) / "evidence.json").read_bytes()

    (repo / "extra_module.py").write_text("X = 1\n", encoding="utf-8")
    gate = _refusal(repo)

    assert gate["gate_state"] != "COMPLETED_CLEAN"
    assert gate["gate_state"] == gate_states.GateState.REFUSED.value
    assert gate["authorized_to_ship"] is False
    # the earlier sealed decision is left intact -- no silent rewrite
    after = (common_paths.get_evidence_dir(repo) / "evidence.json").read_bytes()
    assert after == before


def test_orchestrator_maps_a_refusal_to_a_refusal(tmp_path: Path) -> None:
    """The lifecycle path must report what the gate returned, not a default."""
    repo = _make_repo(tmp_path)
    ship_gate.gate_ship(repo, reviewer=_reviewer)  # leave a clean artifact
    (repo / "extra_module.py").write_text("X = 1\n", encoding="utf-8")

    gate = orchestrator._run_gate(repo, ())

    assert gate["gate_state"] == gate_states.GateState.REFUSED.value
    assert gate["authorized_to_ship"] is False
    assert any("working tree is dirty" in reason for reason in gate["reasons"])
    # the stage is recorded as executed (PR #10), the verdict is not approval
    assert gate["gate_state"] not in ("", "NOT_EXECUTED")


def test_tampered_prior_artifact_is_invalid_evidence_not_a_refusal(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payload = ship_gate.gate_ship(repo, reviewer=_reviewer)
    path = common_paths.get_evidence_dir(repo) / "evidence.json"
    payload["gate_state"] = gate_states.GateState.COMPLETED_WITH_FINDINGS.value
    path.write_text(json.dumps(payload), encoding="utf-8")

    gate = _refusal(repo)

    assert gate["gate_state"] == gate_states.GateState.INVALID_EVIDENCE.value
    assert gate["authorized_to_ship"] is False


# ------------------------------------------------------------ executed paths

def test_clean_execution_still_authorizes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    sealed = ship_gate.gate_ship(repo, reviewer=_reviewer)

    assert sealed["gate_state"] == "COMPLETED_CLEAN"
    assert sealed["authorized_to_ship"] is True
    assert _checks(repo, {"name": "ruff"})[0]["status"] == "passed"


def test_failed_checks_refuse_and_keep_the_real_exit_code(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "tests/test_ok.py").write_text(
        "def test_ok():\n    assert False\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "break")

    gate = _refusal(repo)

    assert gate["gate_state"] == gate_states.GateState.EXECUTION_FAILED.value
    assert gate["authorized_to_ship"] is False
    # the reason names the check that failed; an empty list hid this once
    assert any("pytest" in reason for reason in gate["reasons"])
    assert _checks(repo, {"name": "pytest"})[0]["exit_code"] == 1


def test_a_check_without_an_exit_code_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)

    def _run(root: Path, resolved: dict, **_: object) -> list[dict]:
        return [{"name": "ruff", "status": "passed", "exit_code": None, "note": ""},
                {"name": "pytest", "status": "passed", "exit_code": 0, "note": ""},
                {"name": "mypy", "status": "passed", "exit_code": 0, "note": ""}]

    monkeypatch.setattr(ship_gate.check_runner, "run", _run)
    gate = _refusal(repo)

    assert gate["gate_state"] == gate_states.GateState.INVALID_EVIDENCE.value
    assert gate["authorized_to_ship"] is False


def test_a_timed_out_check_is_a_failure_not_a_completion(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)

    def _run(root: Path, resolved: dict, **_: object) -> list[dict]:
        return [{"name": "ruff", "status": "passed", "exit_code": 0, "note": ""},
                {"name": "pytest", "status": "failed", "exit_code": None,
                 "note": "timed out after 1s"},
                {"name": "mypy", "status": "passed", "exit_code": 0, "note": ""}]

    monkeypatch.setattr(ship_gate.check_runner, "run", _run)
    gate = _refusal(repo)

    assert gate["gate_state"] == gate_states.GateState.EXECUTION_FAILED.value
    assert gate["authorized_to_ship"] is False


# --------------------------------------------------------- state vocabulary

def test_refused_never_authorizes_and_is_never_derived() -> None:
    assert gate_states.GateState.REFUSED.value == "REFUSED"
    assert gate_states.GateState.REFUSED not in gate_states.STATE_ALLOWS_SHIP
    assert gate_states.GateState("REFUSED") is gate_states.GateState.REFUSED


def test_a_refused_gate_survives_json_roundtrip() -> None:
    """The persisted run stores the gate dict as-is; nothing re-derives it."""
    gate = {"gate_state": "REFUSED", "authorized_to_ship": False,
            "reasons": ["working tree is dirty"], "policy_version": "phase2"}
    restored = json.loads(json.dumps(gate))

    assert restored == gate
    assert gate_states.GateState(restored["gate_state"]) is (
        gate_states.GateState.REFUSED)
    assert gate_states.GateState("COMPLETED_CLEAN") is (
        gate_states.GateState.COMPLETED_CLEAN)  # legacy artifacts still load
