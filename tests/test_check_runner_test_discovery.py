"""Regression: pytest test discovery for the non-scoped gate path.

Defect pinned here: ``check_runner._commands`` hardcoded ``pytest tests/ -q``,
so a repository whose suite lives at the root (no ``tests/`` directory) got
pytest exit 4 (usage error: file or directory not found), which the gate
reported as ``EXECUTION_FAILED`` / rc=2 on code that actually passed.

Every case runs a REAL subprocess against an isolated temporary repository
(``tmp_path``), so the assertions cover the runner, not a mock. Each repo is
self-contained: nothing here can collect the developer's working tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from asha import check_runner
from asha.gate_states import CheckResult


def _resolved(*, affected: list[str] | None = None) -> dict:
    return {
        "scope": "S2",
        "checks": ["pytest"],
        "affected_files": affected or [],
        "per_file": {},
        "base": None,
    }


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _pytest_argv() -> list[str]:
    return [sys.executable, "-m", "pytest"]


# ---------------------------------------------------------------------------
# command shape
# ---------------------------------------------------------------------------


def test_tests_dir_present_keeps_configured_target(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_a.py", "def test_a():\n    assert True\n")
    commands = check_runner._commands(tmp_path, ["pytest"], [])
    assert commands == [("pytest", [*_pytest_argv(), "tests/", "-q"])]


def test_no_tests_dir_passes_no_path(tmp_path: Path) -> None:
    _write(tmp_path / "test_root.py", "def test_root():\n    assert True\n")
    commands = check_runner._commands(tmp_path, ["pytest"], [])
    assert commands == [("pytest", [*_pytest_argv(), "-q"])]


# ---------------------------------------------------------------------------
# real subprocess behaviour
# ---------------------------------------------------------------------------


def test_root_level_passing_suite_validates(tmp_path: Path) -> None:
    """THE defect: passing root-level suite must PASS, not EXECUTION_FAILED."""
    _write(tmp_path / "test_root.py", "def test_root():\n    assert 1 + 1 == 2\n")
    (entry,) = check_runner.run(tmp_path, _resolved())
    assert entry["status"] == "passed", entry
    assert entry["exit_code"] == 0, entry
    assert "note" not in entry, entry
    assert "1 passed" in entry["output_tail"], entry


def test_tests_dir_passing_suite_still_validates(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_a.py", "def test_a():\n    assert True\n")
    (entry,) = check_runner.run(tmp_path, _resolved())
    assert entry["status"] == "passed", entry
    assert entry["exit_code"] == 0, entry


def test_root_level_failing_test_is_validation_failure(tmp_path: Path) -> None:
    _write(tmp_path / "test_root.py", "def test_root():\n    assert False\n")
    (entry,) = check_runner.run(tmp_path, _resolved())
    assert entry["status"] == "failed", entry
    assert entry["exit_code"] == 1, entry
    assert entry["note"] == "test_failures", entry


def test_project_config_testpaths_honoured_without_tests_dir(
    tmp_path: Path,
) -> None:
    """No ``tests/`` dir: discovery must follow the project config."""
    _write(tmp_path / "pytest.ini", "[pytest]\ntestpaths = suite\n")
    _write(tmp_path / "suite" / "test_s.py", "def test_s():\n    assert True\n")
    (entry,) = check_runner.run(tmp_path, _resolved())
    assert entry["status"] == "passed", entry
    assert "1 passed" in entry["output_tail"], entry


def test_config_error_is_execution_error_not_pass(tmp_path: Path) -> None:
    """A pytest usage/config error (exit 4) stays an explicit failure."""
    _write(tmp_path / "test_root.py", "def test_root():\n    assert True\n")
    _write(tmp_path / "pytest.ini", "[pytest]\naddopts = --no-such-flag\n")
    (entry,) = check_runner.run(tmp_path, _resolved())
    assert entry["status"] == "failed", entry
    assert entry["exit_code"] == 4, entry
    assert entry["note"] == "pytest_usage_or_config_error", entry
    # and the authorization contract cannot read it as a proven skip
    contract = CheckResult(name="pytest", status=entry["status"],
                           exit_code=entry["exit_code"],
                           note=entry.get("note", ""))
    assert contract.is_proven_skip is False


def test_missing_runner_is_execution_error_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing interpreter/pytest = OSError -> failed (-1), never a pass."""
    _write(tmp_path / "test_root.py", "def test_root():\n    assert True\n")
    monkeypatch.setattr(
        check_runner.sys, "executable", str(tmp_path / "no-such-python"))
    (entry,) = check_runner.run(tmp_path, _resolved())
    assert entry["status"] == "failed", entry
    assert entry["exit_code"] == -1, entry


# ---------------------------------------------------------------------------
# exit-code vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "note"),
    [
        (1, "test_failures"),
        (2, "pytest_interrupted"),
        (3, "pytest_internal_error"),
        (4, "pytest_usage_or_config_error"),
        (5, "no_tests_collected"),
        (9, "pytest_failed"),
    ],
)
def test_pytest_exit_note_mapping(code: int, note: str) -> None:
    assert check_runner._pytest_exit_note(code) == note
