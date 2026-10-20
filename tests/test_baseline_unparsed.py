"""Benchmark Task C regression: a baseline that failed without parseable
failure identities is UNKNOWN, never an empty baseline.

Shape: tests/test_calc.py imports `add`, which does not exist at the base
revision -> pytest exits 1 on a collection ERROR with no `FAILED ...` line.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from asha.governance import delta
from asha.governance import worker_execution as we
from asha.governance.dag import run_workers_dag

PY = sys.executable
CALC_BASE = "def mul(a, b):\n    return a * b\n"
CALC_WITH_ADD = CALC_BASE + "\n\ndef add(a, b):\n    return a + b\n"
TEST_CALC = ("from calc import add, mul\n\n\n"
             "def test_mul():\n    assert mul(3, 4) == 12\n\n\n"
             "def test_add():\n    assert add(2, 3) == 5\n")
TEST_CALC_MUL = ("from calc import mul\n\n\n"
                 "def test_mul():\n    assert mul(3, 4) == 12\n")
TEST_LEGACY = "def test_legacy():\n    assert 1 == 2\n"
PASSING = [{"name": "pytest", "status": "passed", "exit_code": 0,
            "output_tail": "2 passed in 0.01s"}]
COLLECTION_ERROR = [{
    "name": "pytest", "status": "failed", "exit_code": 1,
    "output_tail": ("=========================== short test summary info "
                    "===========================\nERROR tests/test_calc.py\n"
                    "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection "
                    "!!!!!!!!!!!!!!!!!!!\n1 error in 0.24s\n")}]


def _repo(tmp_path: Path, *, legacy: bool,
          test_calc: str = TEST_CALC) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fx"\n')
    (tmp_path / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.ruff_cache/\n.mypy_cache/\n")
    (tmp_path / "calc.py").write_text(CALC_BASE)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(test_calc)
    if legacy:
        (tmp_path / "tests" / "test_legacy.py").write_text(TEST_LEGACY)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "base"],
                   check=True, capture_output=True)
    return tmp_path


def _worker() -> dict:
    return {"id": "w1", "deps": [],
            "declared_scope": ["calc.py"], "reads": ["calc.py"],
            "writes": ["calc.py"],
            "cmd": [PY, "-c", "".join([
                "import pathlib;pathlib.Path('calc.py').write_text(",
                f"{CALC_WITH_ADD!r})"])]}


def test_collection_error_baseline_is_unknown_not_empty(tmp_path: Path) -> None:
    root = _repo(tmp_path, legacy=True)
    report = run_workers_dag(root, [_worker()], task_id="t-c")
    st = report["states"]["w1"]

    assert st["state"] == "FAILED", st          # no DONE via the exemption
    assert st["verdict"] == "UNKNOWN", st       # not NEW_FAILURES either
    cmp = st["comparison"]
    assert cmp["performed"] is False, cmp
    assert cmp["comparable"] is False, cmp
    assert cmp["reason"] == "NOT_COMPARED", cmp
    assert cmp["detail"] == "baseline_failures_unparsed", cmp
    assert st.get("baseline_failures") is None, st   # unknown, not []
    assert not report["evidence"], report            # nothing sealed


def test_passing_baseline_still_gives_empty_failure_set(tmp_path: Path) -> None:
    # collectable at base: the baseline here is genuinely green
    root = _repo(tmp_path, legacy=False, test_calc=TEST_CALC_MUL)
    report = run_workers_dag(root, [_worker()], task_id="t-green")
    st = report["states"]["w1"]

    assert st["state"] == "DONE", st
    assert st["comparison"]["performed"] is True, st
    assert st["comparison"]["verdict"] == "NO_FAILURES", st
    assert st["baseline_failures"] == [], st
    assert st["delta_failures"] == [], st


def test_ordinary_failing_baseline_still_compares(tmp_path: Path,
                                                  ) -> None:
    root = _repo(tmp_path, legacy=True)
    (root / "tests" / "test_legacy.py").write_text(TEST_LEGACY)  # red but parsed
    (root / "tests" / "test_calc.py").write_text(
        "from calc import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "parsable"],
                   check=True, capture_output=True)
    report = run_workers_dag(root, [_worker()], task_id="t-parsable")
    st = report["states"]["w1"]

    assert st["state"] == "DONE", st
    assert st["comparison"]["performed"] is True, st
    assert st["verdict"] == "PRE_EXISTING_ONLY", st
    assert st["delta_failures"] == [], st
    assert [f["location"] for f in st["baseline_failures"]] == [
        "tests/test_legacy.py::test_legacy"]


def test_build_comparison_rejects_unparsed_baseline() -> None:
    record = we.build_comparison(
        base_tree="a" * 40, candidate_tree="b" * 40,
        baseline_checks=COLLECTION_ERROR,
        candidate_checks=[{"name": "pytest", "status": "passed"}])
    assert record["performed"] is False
    assert record["comparable"] is False
    assert record["reason"] == "NOT_COMPARED"
    assert record["detail"] == "baseline_failures_unparsed"
    assert record["verdict"] is None

    ok = we.build_comparison(
        base_tree="a" * 40, candidate_tree="b" * 40,
        baseline_checks=PASSING,
        candidate_checks=[{"name": "pytest", "status": "passed"}],
        verdict="NO_FAILURES")
    assert ok["performed"] is True and ok["detail"] is None
    assert delta.unparsed_failures(PASSING) is False
    assert delta.unparsed_failures(COLLECTION_ERROR) is True


# ------------------- R1: unknown baseline cannot authorize DONE
def test_green_candidate_with_unknown_baseline_is_not_done(
        tmp_path: Path) -> None:
    """Task A shape: candidate checks pass, baseline cannot be collected."""
    root = _repo(tmp_path, legacy=False)          # test_calc imports `add`
    report = run_workers_dag(root, [_worker()], task_id="t-r1")
    st = report["states"]["w1"]

    assert st["state"] == "FAILED", st
    assert st["reason"] == (
        "comparison_inconclusive:baseline_failures_unparsed"), st
    assert st.get("evidence") is None, st          # no success seal
    assert st["comparison"]["performed"] is False, st
    assert not report["evidence"], report


def test_dependent_worker_not_authorized_by_inconclusive_upstream(
        tmp_path: Path) -> None:
    root = _repo(tmp_path, legacy=False)
    report = run_workers_dag(root, [_worker(), {
        "id": "w2", "deps": ["w1"], "declared_scope": ["other.py"],
        "reads": ["other.py"], "writes": ["other.py"],
        "cmd": [PY, "-c",
                "import pathlib;pathlib.Path('other.py').write_text('X = 1\n')"],
    }], task_id="t-r1-dep")
    states = report["states"]

    assert states["w1"]["state"] == "FAILED", states["w1"]
    assert states["w2"]["state"] != "DONE", states["w2"]


def test_no_change_run_still_done(tmp_path: Path) -> None:
    """BASE_EQUALS_TARGET is a definite result, not an unknown one."""
    root = _repo(tmp_path, legacy=False, test_calc=TEST_CALC_MUL)
    worker = _worker()
    worker["cmd"] = [PY, "-c", "".join([
        "import pathlib;pathlib.Path('calc.py').write_text(",
        f"{CALC_BASE!r})"])]
    report = run_workers_dag(root, [worker], task_id="t-same")
    st = report["states"]["w1"]

    assert st["comparison"]["reason"] == "BASE_EQUALS_TARGET", st
    assert st["comparison"]["performed"] is False, st
    assert st["state"] == "DONE", st
    assert report["evidence"].get("w1"), report


def test_cli_reports_inconclusive_run_without_sealing(
        tmp_path: Path) -> None:
    root = _repo(tmp_path, legacy=False)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"task_id": "t-cli", "workers": [_worker()]}))
    proc = subprocess.run(
        [PY, "-m", "asha", "--root", str(root), "run", "--spec", str(spec)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=300)
    report = json.loads(proc.stdout)

    assert proc.returncode != 0, proc.stdout[-400:]
    st = report["states"]["w1"]
    assert st["state"] == "FAILED", st
    assert st["reason"].startswith("comparison_inconclusive:"), st
    assert not report["evidence"], report
    assert report["status"] != "ok" or report.get("error")


# --------------------- audit: BASE_EQUALS_TARGET is a real identity
def test_changed_tree_never_reports_base_equals_target(tmp_path: Path) -> None:
    root = _repo(tmp_path, legacy=False, test_calc=TEST_CALC_MUL)
    report = run_workers_dag(root, [_worker()], task_id="t-changed")
    cmp = report["states"]["w1"]["comparison"]

    assert cmp["baseline_tree"] != cmp["candidate_tree"], cmp
    assert cmp["reason"] == "COMPARED", cmp
    assert cmp["performed"] is True, cmp


def test_cli_reports_same_diagnostic_with_and_without_keep_worktrees(
        tmp_path: Path) -> None:
    seen = []
    for keep in (True, False):
        parent = tmp_path / ("keep" if keep else "drop")
        parent.mkdir(parents=True, exist_ok=True)
        root = _repo(parent, legacy=False)
        spec = root / "spec.json"
        spec.write_text(json.dumps({"task_id": f"t-cli-{keep}",
                                    "workers": [_worker()]}))
        argv = [PY, "-m", "asha", "--root", str(root), "run",
                "--spec", str(spec)]
        if keep:
            argv.append("--keep-worktrees")
        proc = subprocess.run(
            argv, cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=300)
        report = json.loads(proc.stdout)
        st = report["states"]["w1"]
        seen.append((proc.returncode, st["state"], st["reason"],
                     st["comparison"]["reason"],
                     st["comparison"]["detail"], bool(report["evidence"])))

    assert seen[0] == seen[1], seen
    assert seen[0][1] == "FAILED", seen
    assert seen[0][2] == "comparison_inconclusive:baseline_failures_unparsed"
    assert seen[0][4] == "baseline_failures_unparsed"
    assert seen[0][5] is False                      # nothing sealed either way
