"""Phase 9 -- an artifact must not be accepted under another task's identity.

The storage layout is ``<orchestrator>/<task>/<worker>.json``; the directory is
a namespace, not a validation. Phase 8 bound the artifact to its worker; this
binds it to the task the caller is sealing, so a copy of task A's artifact
dropped into task B's namespace is refused instead of silently reconciling.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha import evidence
from asha.governance.worker_execution import verify_worker_evidence
from asha.scheduler import GovernedScheduler
from asha.types import OrchestratorError

PY = sys.executable

BASELINE = {
    ".gitignore": (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
                   ".mypy_cache/\n.ruff_cache/\n"),
    "README.md": "# fixture\n",
    "pkg/__init__.py": "",
    "pkg/module_a.py": 'VALUE_A = "base a"\n',
    "pkg/module_b.py": 'VALUE_B = "base b"\n',
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True).stdout.strip()


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in BASELINE.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _worker(wid: str, rel: str, text: str) -> dict:
    return {"id": wid, "deps": [], "declared_scope": [rel],
            "reads": [rel], "writes": [rel],
            "cmd": [PY, "-c",
                    (f"from pathlib import Path; "
                     f"Path({rel!r}).write_text({text!r})")]}


def _run(repo: Path, task: str, wid: str = "wa") -> Path:
    sched = GovernedScheduler(
        repo, [_worker(wid, "pkg/module_a.py", 'VALUE_A = "changed"\n')],
        task_id=task)
    report = sched.run()
    assert report["status"] == "ok", (report["states"], report["reason"])
    return Path(report["evidence"][wid])


@pytest.fixture()
def tasks(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Two real task namespaces over the same repository."""
    repo = _make_repo(tmp_path)
    task_a = _run(repo, "task-a")
    task_b = _run(repo, "task-b")
    return repo, task_a, task_b


def test_same_worker_id_in_two_tasks_stays_bound_to_its_task(
        tasks: tuple[Path, Path, Path]) -> None:
    _repo, task_a, task_b = tasks
    assert task_a.parent.name == "task-a"
    assert task_b.parent.name == "task-b"

    # each artifact is accepted only under its own task
    assert verify_worker_evidence(
        task_a, expected_worker_id="wa", expected_task_id="task-a")["task_id"] \
        == "task-a"
    assert verify_worker_evidence(
        task_b, expected_worker_id="wa", expected_task_id="task-b")["task_id"] \
        == "task-b"

    # A's artifact inside B's namespace is still A's evidence
    with pytest.raises(OrchestratorError, match="task mismatch"):
        verify_worker_evidence(task_a, expected_worker_id="wa",
                               expected_task_id="task-b")


def test_cross_task_artifact_substitution_is_rejected(
        tasks: tuple[Path, Path, Path]) -> None:
    _repo, task_a, task_b = tasks
    victim = task_b.parent / "wa.json"
    shutil.copyfile(task_a, victim)          # byte-identical, wrong task

    with pytest.raises(OrchestratorError, match="task mismatch"):
        verify_worker_evidence(victim, expected_worker_id="wa",
                               expected_task_id="task-b")
    # the artifact is intact as its own task's evidence: nothing was rewritten
    assert verify_worker_evidence(
        task_a, expected_worker_id="wa", expected_task_id="task-a")["task_id"] \
        == "task-a"
    assert json.loads(task_a.read_text(encoding="utf-8"))["task_id"] == "task-a"


def test_worker_identity_and_task_identity_are_independent_checks(
        tasks: tuple[Path, Path, Path]) -> None:
    _repo, task_a, _task_b = tasks
    # right task, wrong worker
    with pytest.raises(OrchestratorError, match="worker evidence identity"):
        verify_worker_evidence(task_a, expected_worker_id="wb",
                               expected_task_id="task-a")
    # right worker, wrong task
    with pytest.raises(OrchestratorError, match="task mismatch"):
        verify_worker_evidence(task_a, expected_worker_id="wa",
                               expected_task_id="task-z")
    # both right
    assert verify_worker_evidence(
        task_a, expected_worker_id="wa", expected_task_id="task-a")["worker_id"] \
        == "wa"


def test_task_identity_is_not_taken_from_the_artifact(
        tasks: tuple[Path, Path, Path]) -> None:
    """The check must compare against the caller, never against the payload."""
    _repo, task_a, _task_b = tasks
    payload = json.loads(task_a.read_text(encoding="utf-8"))
    forged = dict(payload, task_id="task-b")
    forged["evidence_sha256"] = evidence.compute_digest(forged)
    forged_path = task_a.parent / "forged.json"
    forged_path.write_text(json.dumps(forged, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")

    # self-consistent forgery: the sealed claim says task-b, the caller is not
    with pytest.raises(OrchestratorError, match="task mismatch"):
        verify_worker_evidence(forged_path, expected_task_id="task-a")
    assert verify_worker_evidence(forged_path,
                                  expected_task_id="task-b")["task_id"] == "task-b"
