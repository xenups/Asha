"""P-C: a governed acceptance command must be *executed* and recorded.

Regression for the 0.5.0 defect where `DAGCoordinator` bound its execution
hook to `default_execute` (no runner audit), so every supplied
`verify_command` reached `build_acceptance` as `not_executed` and the run
failed closed with `acceptance_failed` even when the command had passed.

The contract under test: the exit code in the acceptance record is the real
exit code of a real process; a command that did not run is never reported as
passed; a pass is never sealed without a passing command.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from asha.governance import dag
from asha.types import OrchestratorError

PY = sys.executable
FILES = {"a.txt": "one\n", "test_ok.py": "def test_ok():\n    assert True\n"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@example.invalid")
    _git(repo, "config", "user.name", "A")
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _worker(*, verify: str | None, name: str = "w1",
            exit_code: int = 0) -> dict:
    worker: dict = {"id": name, "deps": [], "declared_scope": ["out.txt"],
                    "reads": [], "writes": ["out.txt"]}
    body = f"open('out.txt','w').write('ok'); raise SystemExit({exit_code})"
    worker["cmd"] = [PY, "-c", body]
    if verify is not None:
        worker["verify_command"] = verify
    return worker


def _run(repo: Path, workers: list[dict], task_id: str) -> dict:
    return dag.run_workers_dag(repo, workers, task_id=task_id)


def _sealed(report: dict, wid: str = "w1") -> dict:
    return json.loads(Path(report["evidence"][wid]).read_text(encoding="utf-8"))


def test_passing_acceptance_is_executed_and_recorded(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = _run(repo, [_worker(
        verify=f'{PY} -c "print(\'3 passed\'); raise SystemExit(0)"')], "p1")
    state = report["states"]["w1"]
    assert report["status"] == "ok", state
    assert state["state"] == "DONE"
    acceptance = state["acceptance"]
    assert acceptance["supplied"] is True
    assert acceptance["ran"] is True
    assert acceptance["exit_code"] == 0
    assert acceptance["outcome"] == "passed"
    assert acceptance["detail"] == "3 passed"
    assert _sealed(report)["acceptance"]["outcome"] == "passed"


def test_failing_acceptance_is_a_real_nonzero_exit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = _run(repo, [_worker(
        verify=f'{PY} -c "print(\'1 failed\'); raise SystemExit(3)"')], "p2")
    state = report["states"]["w1"]
    assert report["status"] != "ok"
    assert state["state"] == "FAILED"
    assert state["reason"] == "acceptance_failed"
    assert state["acceptance"]["ran"] is True
    assert state["acceptance"]["exit_code"] == 3
    # exit 3 is pytest's INTERNAL ERROR: the runner never produced a verdict,
    # so the honest outcome is "inconclusive" (still refusing DONE / no seal).
    assert state["acceptance"]["outcome"] == "inconclusive"
    assert "w1" not in report["evidence"]


def test_acceptance_that_never_ran_is_not_a_pass(tmp_path: Path) -> None:
    """Worker fails first: the acceptance command cannot have run."""
    repo = _repo(tmp_path)
    report = _run(repo, [_worker(verify=f'{PY} -c "print(\'1 passed\')"',
                                 exit_code=2)], "p3")
    state = report["states"]["w1"]
    assert state["state"] == "FAILED"
    assert state["acceptance"]["supplied"] is True
    assert state["acceptance"]["ran"] is False
    assert state["acceptance"]["exit_code"] is None
    assert state["acceptance"]["outcome"] == "not_executed"
    assert "w1" not in report["evidence"]


def test_injected_hook_cannot_fake_a_passing_acceptance(tmp_path: Path) -> None:
    """A 2-tuple hook carries no audit: acceptance stays unproven."""
    repo = _repo(tmp_path)
    report = dag.run_workers_dag(
        repo, [_worker(verify=f'{PY} -c "print(\'1 passed\')"')],
        task_id="p4",
        execute_hook=lambda worker, path: (0, ""))
    state = report["states"]["w1"]
    assert state["state"] == "FAILED"
    assert state["reason"] == "acceptance_failed"
    assert state["acceptance"]["ran"] is False
    assert state["acceptance"]["outcome"] == "not_executed"


def test_no_tests_collected_is_inconclusive(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = _run(repo, [_worker(
        verify=f'{PY} -c "raise SystemExit(5)"')], "p5")
    state = report["states"]["w1"]
    assert state["acceptance"]["ran"] is True
    assert state["acceptance"]["exit_code"] == 5
    assert state["acceptance"]["outcome"] == "inconclusive"
    assert state["state"] == "FAILED"


def test_skipped_only_acceptance_is_inconclusive(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = _run(repo, [_worker(
        verify=f'{PY} -c "print(\'0 passed, 4 skipped\')"')], "p6")
    state = report["states"]["w1"]
    assert state["acceptance"]["exit_code"] == 0
    assert state["acceptance"]["outcome"] == "inconclusive"
    assert state["state"] == "FAILED"
    assert state["reason"] == "acceptance_failed"
    assert _sealed(report)["acceptance"]["outcome"] == "inconclusive"


def test_absent_acceptance_is_not_required_but_never_invented(
        tmp_path: Path) -> None:
    """No verify_command: acceptance is not_required, never a fake pass."""
    repo = _repo(tmp_path)
    report = _run(repo, [_worker(verify=None)], "p7")
    state = report["states"]["w1"]
    assert state["acceptance"]["supplied"] is False
    assert state["acceptance"]["ran"] is False
    assert state["acceptance"]["outcome"] == "not_required"
    # the sealed evidence carries no invented acceptance verdict
    assert _sealed(report)["acceptance"]["outcome"] == "not_required"
    assert _sealed(report)["authorized_to_ship"] is False


def test_blank_verify_command_is_rejected_before_dispatch(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(OrchestratorError):
        _run(repo, [_worker(verify="   ")], "p7b")


def test_acceptance_output_reaches_the_sealed_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = _run(repo, [_worker(
        verify=f'{PY} -c "print(\'7 passed in 0.01s\')"')], "p8")
    sealed = _sealed(report)
    assert sealed["acceptance"]["detail"] == "7 passed"
    assert sealed["acceptance"]["command"].startswith(PY)
    # evidence is bound to the candidate tree the acceptance ran against
    assert sealed["target_tree_sha"] == sealed["tree_hash"]


def test_single_worker_facade_refuses_unproven_acceptance(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP/single-worker path enforces the same rule as the DAG."""
    from asha.governance import worker_execution as we

    repo = _repo(tmp_path)
    monkeypatch.setattr(
        we, "execute_with_audit",
        lambda worker, path, deadline=None: (
            0, "", {"verify_ran": False, "verify_exit": None}))
    worker = _worker(verify=f'{PY} -c "print(\'1 passed\')"')
    report = we.run_worker_in_worktree(repo, worker, task_id="p9")
    state = report["states"]["w1"]
    assert state["state"] == "FAILED"
    assert state["reason"] == "acceptance_failed"
    assert state["acceptance"]["ran"] is False
    # the record is sealed and says exactly that
    sealed = json.loads(Path(state["evidence"]).read_text(encoding="utf-8"))
    assert sealed["acceptance"]["outcome"] == "not_executed"
