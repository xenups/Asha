"""A refusal must tell the operator what actually failed (Phase 10 F2).

Phase 10 measured `comparison_inconclusive:baseline_failures_unparsed` for a
worker that broke the syntax of a source file: the candidate's own ruff
failures were recorded in `current_failures` but never named in `reason`, so
the message pointed at the baseline and said nothing about the file that broke.

The classification is unchanged -- still FAILED, still no seal -- and the
reason now carries the candidate failures and the command's exit status.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from asha.governance import worker_execution as we
from asha.governance.dag import run_workers_dag

PY = sys.executable
CALC_BASE = "def mul(a, b):\n    return a * b\n"
TEST_CALC = ("from calc import add, mul\n\n\n"
             "def test_mul():\n    assert mul(3, 4) == 12\n")
BROKEN = "def mul(a, b):\n    return a * (\n"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fx"\n')
    (tmp_path / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.ruff_cache/\n.mypy_cache/\n")
    (tmp_path / "calc.py").write_text(CALC_BASE)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(TEST_CALC)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "base"],
                   check=True, capture_output=True)
    return tmp_path


def _breaking_worker() -> dict:
    return {"id": "w1", "deps": [],
            "declared_scope": ["calc.py"], "reads": ["calc.py"],
            "writes": ["calc.py"],
            "cmd": [PY, "-c", "".join([
                "import pathlib;pathlib.Path('calc.py').write_text(",
                f"{BROKEN!r})"])]}


def test_failure_digest_names_check_code_and_location() -> None:
    from asha.governance.delta import FailureIdentity

    one = FailureIdentity(check="ruff", location="src/x.py:3:5",
                          code="E999", message="bad")
    assert we.failure_digest([one]) == "ruff:E999@src/x.py:3:5"
    syntax = FailureIdentity(check="ruff", location="src/x.py:9:1",
                             code=None, message="invalid-syntax")
    assert we.failure_digest([syntax]) == "ruff:syntax@src/x.py:9:1"
    assert we.failure_digest([]) == "none"
    many = [FailureIdentity(check="ruff", location=f"src/x.py:{index}:1",
                            code=None, message="m")
            for index in range(1, 6)]
    assert we.failure_digest(many) == (
        "ruff:syntax@src/x.py:1:1,ruff:syntax@src/x.py:2:1,"
        "ruff:syntax@src/x.py:3:1,+2 more")


def test_unparsed_baseline_reason_names_the_candidate_failure(
        tmp_path: Path) -> None:
    """Phase 10 S6b shape: baseline unparsed AND the candidate is broken."""
    root = _repo(tmp_path)
    report = run_workers_dag(root, [_breaking_worker()], task_id="t-f2")
    st = report["states"]["w1"]

    reason = st["reason"]
    assert st["state"] == "FAILED", st
    assert st.get("evidence") is None, st
    assert st["verification_status"] == "COMPARISON_INCONCLUSIVE", st
    assert reason.startswith(
        "comparison_inconclusive:baseline_failures_unparsed"), reason
    assert ";candidate_failures=ruff:" in reason, reason
    assert ";worker_exit=" in reason, reason
    assert "@calc.py:" in reason, reason
    assert st["current_failures"], st


def test_reason_stays_bounded_for_a_broken_tree(tmp_path: Path) -> None:
    """A tree full of syntax errors must not produce an unbounded reason."""
    root = _repo(tmp_path)
    report = run_workers_dag(root, [_breaking_worker()], task_id="t-f2b")
    reason = report["states"]["w1"]["reason"]
    assert len(reason) < 400, reason
    assert reason.count("@") <= 3, reason
