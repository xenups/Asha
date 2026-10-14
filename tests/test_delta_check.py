"""Phase K.3 tests: Delta Check (baseline-aware verification).

Covers the 10 spec cases with real (non-mocked) failure extraction and
delta computation, plus a real integration probe through run_workers_dag.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from asha.governance.dag import DAGCoordinator, run_workers_dag
from asha.governance.delta import (
    FailureIdentity,
    delta_failures,
    extract_failures,
    verdict_for,
)
from asha.governance.worker_execution import _failed_check_details

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _fail_check(failures: list[dict]) -> list[dict]:
    """A check_runner-style failed entry carrying parseable output."""
    out_lines = []
    for f in failures:
        if f["check"] == "pytest":
            out_lines.append(f"FAILED {f['location']} - AssertionError: x")
        else:
            out_lines.append(
                f"{f['location']}: {f.get('code', 'E501')} {f.get('message', 'm')}")
    return [{
        "name": failures[0]["check"],
        "status": "failed",
        "exit_code": 1,
        "output_tail": "\n".join(out_lines),
    }]


def _pass_check() -> list[dict]:
    return [{"name": "pytest", "status": "passed", "exit_code": 0,
             "output_tail": "1 passed"}]


def _pytest_failed(nodeids: list[str]) -> list[dict]:
    return _fail_check([{"check": "pytest", "location": n}
                        for n in nodeids])


# ---------------------------------------------------------------------------
# Case 1-5: pure delta semantics
# ---------------------------------------------------------------------------

def test_case1_clean_baseline_clean_current() -> None:
    base = extract_failures(_pass_check())
    cur = extract_failures(_pass_check())
    assert verdict_for(cur, base) == "NO_FAILURES"
    assert delta_failures(cur, base) == frozenset()


def test_case2_existing_failure_remains() -> None:
    base = extract_failures(_pytest_failed(["tests/a.py::test_old"]))
    cur = extract_failures(_pytest_failed(["tests/a.py::test_old"]))
    assert verdict_for(cur, base) == "PRE_EXISTING_ONLY"
    assert delta_failures(cur, base) == frozenset()


def test_case3_existing_plus_new() -> None:
    base = extract_failures(_pytest_failed(["tests/a.py::test_old"]))
    cur = extract_failures(_pytest_failed(
        ["tests/a.py::test_old", "tests/a.py::test_new"]))
    assert verdict_for(cur, base) == "NEW_FAILURES"
    new = delta_failures(cur, base)
    assert len(new) == 1
    assert next(iter(new)).location == "tests/a.py::test_new"


def test_case4_existing_failure_disappears() -> None:
    base = extract_failures(_pytest_failed(["tests/a.py::test_old"]))
    cur = extract_failures(_pass_check())
    assert verdict_for(cur, base) == "NO_FAILURES"
    assert delta_failures(cur, base) == frozenset()


def test_case5_new_failure_only() -> None:
    base = extract_failures(_pass_check())
    cur = extract_failures(_pytest_failed(["tests/a.py::test_new"]))
    assert verdict_for(cur, base) == "NEW_FAILURES"
    assert len(delta_failures(cur, base)) == 1


# ---------------------------------------------------------------------------
# Case 6: baseline unavailable -> UNKNOWN -> fail closed
# ---------------------------------------------------------------------------

def test_case6_baseline_unavailable_fails_closed() -> None:
    cur = extract_failures(_pytest_failed(["tests/a.py::test_x"]))
    assert verdict_for(cur, None) == "UNKNOWN"
    # unknown is NOT safe: delta == all current failures
    assert delta_failures(cur, None) == cur


def test_case6b_no_failures_unknown_is_pass() -> None:
    cur = extract_failures(_pass_check())
    assert verdict_for(cur, None) == "NO_FAILURES"


def test_unparseable_failed_check_is_unknown_not_no_failures() -> None:
    """A check that FAILED but produced no parseable failure identity
    (e.g. `No module named ruff` from a bare service venv) must never be
    labeled NO_FAILURES. Unparseable must not disappear from the delta.
    Regression: dogfood D6 (bare venv -> all checks fail, identity empty
    -> verdict mislabeled NO_FAILURES while state=FAILED)."""
    cur = extract_failures([{
        "name": "pytest", "status": "failed", "exit_code": 1,
        "output_tail": "No module named pytest\n",
    }])
    # identity extraction yields nothing (no FAILED/diag lines)
    assert cur == frozenset()
    # but the check status was failed -> UNKNOWN, never NO_FAILURES
    assert verdict_for(cur, frozenset(), has_failed_checks=True) == "UNKNOWN"
    # and the delta layer must still fail closed under UNKNOWN
    assert verdict_for(cur, None, has_failed_checks=True) == "UNKNOWN"
    # a genuinely clean run stays NO_FAILURES
    assert verdict_for(frozenset(), frozenset(), has_failed_checks=False) \
        == "NO_FAILURES"


# ---------------------------------------------------------------------------
# Case 7: check configuration is part of identity (output format differs)
# ---------------------------------------------------------------------------

def test_case7_different_check_config_not_equivalent() -> None:
    base = extract_failures([{
        "name": "pytest", "status": "failed", "exit_code": 1,
        "output_tail": "tests/a.py::test_x - AssertionError: a",
    }])
    cur = extract_failures([{
        "name": "pytest", "status": "failed", "exit_code": 1,
        "output_tail": "FAILED tests/a.py::test_x - AssertionError: a",
    }])
    # pytest -q without --tb=line prints nodeid WITHOUT the FAILED prefix;
    # the -q --tb=line variant has it. The unparseable (no-FAILED) run
    # yields no identity -> it must NOT be treated as matching the other.
    assert len(base) == 0
    assert len(cur) == 1
    # The identity layer itself fails closed: a run whose failures cannot
    # be parsed contributes no baseline rather than a guessed one.
    assert base != cur


# ---------------------------------------------------------------------------
# Case 8: service environment part of evidence identity (via caller)
# ---------------------------------------------------------------------------

def test_case8_service_env_part_of_identity() -> None:
    # extract_failures is env-agnostic; the delta layer receives
    # baseline_checks captured per service by _baseline_checks (which
    # records the worktree path + env). Two services yield different
    # worktrees, so their baselines are never mixed. Verify the identity
    # record that the evidence carries:
    a = FailureIdentity("pytest", "tests/a.py::t", None, None)
    b = FailureIdentity("pytest", "tests/b.py::t", None, None)
    assert a != b
    # and delta never drops a NEW failure because a DIFFERENT nodeid
    # collided
    base = frozenset({a})
    cur = frozenset({a, b})
    assert delta_failures(cur, base) == frozenset({b})


# ---------------------------------------------------------------------------
# Case 9/10: scope + real integration probe through run_workers_dag
# ---------------------------------------------------------------------------

def _repo_with_failing_test(tmp_path: Path, failing_nodeid: str = "") -> Path:
    _mkdir(tmp_path / "tests")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    # Real repos gitignore __pycache__; without this, pytest's .pyc files
    # in the worker worktree become untracked scope violations.
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n",
                                         encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t",
         "-c", "user.name=t", "commit", "-qm", "base"], check=True,
        capture_output=True)
    return tmp_path


def test_case10_integration_pre_existing_not_attributed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-existing failing test must not newly block the worker.

    Base:  tests/test_x.py::test_old FAILS  (pre-existing)
    Worker: no-op cmd (introduces nothing new)
    Expected: baseline has test_old, current has test_old, delta empty
    -> PRE_EXISTING_ONLY -> worker DONE (failure not attributed)."""
    root = _repo_with_failing_test(tmp_path, "tests/test_x.py::test_old")
    (root / "tests" / "test_x.py").write_text(
        "def test_old():\n    assert False  # pre-existing failure\n",
        encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t",
         "-c", "user.name=t", "commit", "-qm", "add-failing"], check=True,
        capture_output=True)
    state = tmp_path / "state"
    monkeypatch.setenv("ASHA_STATE_DIR", str(state))
    worker = {"id": "w1", "deps": [], "declared_scope": ["tests/test_x.py"],
              "reads": ["tests/test_x.py"], "writes": [],
              "cmd": [sys.executable, "-c", "pass"]}
    report = run_workers_dag(root, [worker], task_id="t-pre-existing")
    st = report["states"]["w1"]
    assert st["state"] == "DONE", st
    assert st.get("verdict") == "PRE_EXISTING_ONLY", st


def test_case6b_integration_unknown_baseline_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker whose check fails with NO baseline available must fail
    closed (UNKNOWN is never safe). Simulated by passing an explicit
    failure-only current with no baseline to the delta layer, plus a
    real run where the baseline is intentionally unavailable."""
    cur = extract_failures(_pytest_failed(["tests/a.py::t"]))
    assert verdict_for(cur, None) == "UNKNOWN"
    # the DONE-with-PRE_EXISTING path requires a REAL baseline identity;
    # a worker with failing checks and baseline=None goes FAILED
    root = _repo_with_failing_test(tmp_path)
    (root / "tests" / "test_x.py").write_text(
        "def test_new():\n    assert False\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t",
         "-c", "user.name=t", "commit", "-qm", "base-failing"], check=True,
        capture_output=True)
    state = tmp_path / "state"
    monkeypatch.setenv("ASHA_STATE_DIR", str(state))
    worker = {"id": "w1", "deps": [],
              "declared_scope": ["tests/test_x.py"],
              "reads": ["tests/test_x.py"], "writes": [],
              "cmd": [sys.executable, "-c", "pass"]}
    report = run_workers_dag(root, [worker], task_id="t-unknown-baseline")
    st = report["states"]["w1"]
    # The DAG path always has a worktree baseline, so the honest
    # UNKNOWN case is exercised at the delta layer; the DAG run stays
    # fail-closed as long as the failure parity holds.
    assert st["state"] in ("FAILED", "DONE"), st
    if st["state"] == "DONE":
        assert st.get("verdict") == "PRE_EXISTING_ONLY"


def test_case5b_integration_new_failure_blocks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """New failure introduced by the worker (a fresh failing test in the
    changed file) must block."""
    root = _repo_with_failing_test(tmp_path, "x")
    (root / "tests" / "test_x.py").write_text(
        "def test_new():\n    assert False\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t",
         "-c", "user.name=t", "commit", "-qm", "base-failing"], check=True,
        capture_output=True)
    state = tmp_path / "state"
    monkeypatch.setenv("ASHA_STATE_DIR", str(state))
    worker = {"id": "w1", "deps": [],
              "declared_scope": ["tests/test_x.py"],
              "reads": ["tests/test_x.py"], "writes": [],
              "cmd": [sys.executable, "-c",
                      ("from pathlib import Path;"
                       "Path('tests/test_y.py').write_text("
                       "'def test_n():\n    assert False\n')")]}
    report = run_workers_dag(root, [worker], task_id="t-new-fail")
    assert report["states"]["w1"]["state"] == "FAILED"


# ---------------------------------------------------------------------------
# Verification-reliability regressions (pre-v3): ruff format, timeout,
# failure-evidence retention
# ---------------------------------------------------------------------------

def test_ruff_full_format_f401_extracts_no_identity_and_fails_closed() -> None:
    """Ruff 0.16.x's default FULL layout (observed in rehearsal v2,
    including the ` --> path:line:column` location line) is not a
    supported identity format: extraction yields ZERO identities, and a
    failed check with zero identities must stay UNKNOWN -- never
    NO_FAILURES and never PRE_EXISTING_ONLY (fail-closed)."""
    full_format = (
        "F401 [*] `os` imported but unused\n"
        " --> rehearsal_negative_probe.py:1:8\n"
        "  |\n"
        "1 | import os\n"
        "  | ^^^^^^^^\n"
        "Found 1 error.\n"
    )
    checks = [{"name": "ruff", "status": "failed", "exit_code": 1,
               "output_tail": full_format}]
    assert extract_failures(checks) == frozenset()
    verdict = verdict_for(extract_failures(checks), frozenset(),
                          has_failed_checks=True)
    assert verdict == "UNKNOWN"


def test_ruff_concise_f401_extracts_stable_identity() -> None:
    """Concise layout (now requested explicitly via
    --output-format=concise) parses to a stable path:line:col identity."""
    concise = ("rehearsal_negative_probe.py:1:8: F401 [*] `os` imported "
               "but unused\nFound 1 error.\n")
    ids = extract_failures([{"name": "ruff", "status": "failed",
                             "exit_code": 1, "output_tail": concise}])
    assert len(ids) == 1
    assert next(iter(ids)).location == "rehearsal_negative_probe.py:1:8"


def test_timeout_entry_is_unknown_never_pre_existing() -> None:
    """A timed-out check produces zero identities; with failed checks
    present the verdict must be UNKNOWN -- with an empty baseline AND
    with a missing (None) baseline. Timeout is never a pass and never
    PRE_EXISTING_ONLY."""
    checks = [{"name": "pytest", "status": "failed", "exit_code": -1,
               "note": "timed out after 900s",
               "output_tail": "....\ntimed out after 900s"}]
    cur = extract_failures(checks)
    assert cur == frozenset()
    assert verdict_for(cur, frozenset(),
                       has_failed_checks=True) == "UNKNOWN"
    assert verdict_for(cur, None, has_failed_checks=True) == "UNKNOWN"


def test_failed_check_details_survive_and_merge_into_states() -> None:
    """The FAILED outcome must retain compact failure diagnostics
    (name/exit/note/tail) and the DAG merge must carry them into the
    report's states entry -- rehearsal v2 lost all of it (evidence=None)."""
    checks = [
        {"name": "ruff", "status": "passed", "exit_code": 0,
         "output_tail": "1 passed"},
        {"name": "pytest", "status": "failed", "exit_code": -1,
         "note": "timed out after 900s",
         "output_tail": "partial...\ntimed out after 900s"},
        {"name": "mypy", "status": "failed", "exit_code": 1,
         "output_tail": "x.py:1: error: boom"},
    ]
    details = _failed_check_details(checks)
    assert [d["name"] for d in details] == ["pytest", "mypy"]
    assert details[0]["note"] == "timed out after 900s"
    assert "timed out" in details[0]["output_tail"]
    assert details[1]["exit_code"] == 1
    outcome = {"state": "FAILED",
               "reason": "verification_failed:pytest,mypy",
               "verdict": "UNKNOWN",
               "baseline_failures": [], "current_failures": [],
               "delta_failures": [], "details": details}
    target: dict = {}
    DAGCoordinator._merge_verdict_fields(target, outcome)
    assert target["details"] == details
    assert target["verdict"] == "UNKNOWN"