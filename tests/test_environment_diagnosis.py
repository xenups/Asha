"""An environment gap must never look like a code failure or a pass.

The reported incident: pytest aborted in collection because the target
project's dependency was missing from the interpreter Asha used, and the run
reported the generic `baseline_failures_unparsed`. The classification is
still fail-closed (FAILED, no seal), but now it names the missing module, the
interpreter that lacked it and the fix -- and an unattributable comparison can
no longer pass silently.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from asha.governance import check_diagnosis
from asha.governance import worker_execution as we
from asha.governance.dag import run_workers_dag

PY = sys.executable
MISSING = "asha_missing_dep_probe"


def _entry(**over) -> dict:
    base = {"name": "pytest", "status": "failed", "launched": True,
            "exit_code": 2, "interpreter": "/venv/bin/python",
            "hermetic": True, "service_root": "/repo/svc"}
    base.update(over)
    return base


def test_missing_dependency_is_named_not_swallowed() -> None:
    diagnosis = check_diagnosis.classify(_entry(output_tail=(
        "ImportError while importing test module 'tests/test_app.py'.\n"
        f"E   ModuleNotFoundError: No module named '{MISSING}'\n"
        "!!!!!!!! Interrupted: 1 error during collection !!!!!!!!\n")))
    assert diagnosis is not None
    assert diagnosis.kind == check_diagnosis.MISSING_DEPENDENCY
    assert diagnosis.module == MISSING
    assert diagnosis.is_environment
    hint = diagnosis.hint()
    assert MISSING in hint and ".venv" in hint


def test_repository_local_import_is_a_collection_error(tmp_path: Path) -> None:
    (tmp_path / "local_mod").mkdir()
    diagnosis = check_diagnosis.classify(
        _entry(output_tail="E   ModuleNotFoundError: No module named 'local_mod'"),
        repo_root=tmp_path)
    assert diagnosis is not None
    assert diagnosis.kind == check_diagnosis.COLLECTION_ERROR


def test_failed_assertions_stay_code_failures() -> None:
    diagnosis = check_diagnosis.classify(_entry(
        exit_code=1, output_tail="FAILED tests/test_app.py::test_x - assert 1 == 2"))
    assert diagnosis is not None
    assert diagnosis.kind == check_diagnosis.TEST_FAILURES
    assert not diagnosis.is_environment


def test_a_passing_check_has_no_diagnosis() -> None:
    assert check_diagnosis.classify(_entry(status="passed")) is None


def test_baseline_environment_gap_is_attributed_to_the_environment() -> None:
    baseline = [_entry(output_tail=(
        f"ModuleNotFoundError: No module named '{MISSING}'"))]
    comparison = we.build_comparison(
        base_tree="a" * 40, candidate_tree="b" * 40,
        baseline_checks=baseline,
        candidate_checks=[{"name": "pytest", "status": "passed",
                           "interpreter": "/venv/bin/python"}],
        baseline_scope="package", candidate_scope="package",
        verdict="NO_FAILURES")
    assert comparison["performed"] is False
    assert comparison["detail"] == "baseline_missing_dependency"
    assert comparison["diagnosis"]["module"] == MISSING
    assert ".venv" in comparison["hint"]


def test_two_interpreters_are_not_a_comparison() -> None:
    baseline = [{"name": "pytest", "status": "passed",
                 "interpreter": "/project/.venv/bin/python"}]
    candidate = [{"name": "pytest", "status": "passed",
                  "interpreter": "/asha/.venv/bin/python"}]
    comparison = we.build_comparison(
        base_tree="a" * 40, candidate_tree="b" * 40,
        baseline_checks=baseline, candidate_checks=candidate,
        baseline_scope="package", candidate_scope="package",
        verdict="NO_FAILURES")
    assert comparison["performed"] is False
    assert comparison["reason"] == "INTERPRETER_MISMATCH"
    assert comparison["interpreter_drift"] == ["pytest"]


def test_every_check_entry_records_its_environment(tmp_path: Path) -> None:
    """Both check paths must carry identity: the scoped path builds its own
    entries, and a comparison cannot attribute a gap without them."""
    from asha import check_runner

    repo = _repo(tmp_path)
    entries = check_runner.run_scoped(
        repo, changed_files=["tests/test_calc.py"],
        targeted_tests=["tests/test_calc.py"], mypy_targets=[])
    assert entries
    for entry in entries:
        assert entry["interpreter"], entry
        assert entry["service_root"], entry
        assert isinstance(entry["hermetic"], bool)
    failed = [e for e in entries if e["status"] == "failed"]
    assert failed, entries
    pytest_entry = next(e for e in failed if e["name"] == "pytest")
    assert pytest_entry["failure_mode"] == check_diagnosis.MISSING_DEPENDENCY
    assert pytest_entry["failure_module"] == MISSING


def _repo(tmp_path: Path) -> Path:
    """Base revision whose tests import a dependency nobody installed."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fx"\n')
    (tmp_path / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.ruff_cache/\n.mypy_cache/\n")
    (tmp_path / "calc.py").write_text("def mul(a, b):\n    return a * b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        f"from {MISSING} import thing\nfrom calc import mul\n\n\n"
        "def test_mul():\n    assert mul(3, 4) == 12\n")
    for args in (["git", "init", "-q", str(tmp_path)],
                 ["git", "-C", str(tmp_path), "add", "-A"]):
        subprocess.run(args, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "base"],
                   check=True, capture_output=True)
    return tmp_path


def _fixing_worker() -> dict:
    """Removes the un-installable import: candidate checks can pass."""
    fixed = ("from calc import mul\n\n\n"
             "def test_mul():\n    assert mul(3, 4) == 12\n")
    return {"id": "w1", "deps": [], "declared_scope": ["tests/test_calc.py"],
            "reads": ["tests/test_calc.py"],
            "writes": ["tests/test_calc.py"],
            "cmd": [PY, "-c", "".join([
                ("import pathlib;pathlib.Path('tests/test_calc.py')"
                 f".write_text({fixed!r})")])]}


def test_a_missing_dependency_never_becomes_a_pass(tmp_path: Path) -> None:
    """Zero-exit worker + green candidate cannot hide a broken baseline."""
    repo = _repo(tmp_path)
    report = run_workers_dag(repo, [_fixing_worker()], task_id="env-1")
    state = report["states"]["w1"]
    assert state["state"] != "DONE"
    assert state["reason"].startswith("comparison_inconclusive:")
    assert "baseline_missing_dependency" in state["reason"]
    assert "hint=" in state["reason"]
    assert "w1" not in (report.get("evidence") or {})
    assert state["comparison"]["performed"] is False
