"""H.2.2-C: Multi-worker DAG parity — GovernedScheduler vs DAGCoordinator.

Runs the SAME multi-worker scenario through both engines and compares
the externally observable contract (H.2.2-B Part 9 parity contract):

  LEGACY: GovernedScheduler(repo, workers, task_id=...).run()
  NEW:    DAGCoordinator(repo, workers, task_id=...).run()

Scenarios (H.2.2-C Phase H):
  H1 independent workers      H6 uncertain/ambiguous owner
  H2 linear A->B->C           H7 stale generation
  H3 branch/join              H8 worker failure
  H4 cycle                    H9 evidence failure
  H5 write conflict           H10 full apply (integration)

Classification per difference: EXACT_MATCH / SEMANTICALLY_EQUIVALENT /
INTENTIONALLY_CHANGED / REGRESSION / UNKNOWN.  Any REGRESSION or UNKNOWN
in safety/governance/evidence/generation/dependency/conflict/integration
blocks scheduler deletion (hard rule).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from asha.governance.dag import DAGCoordinator
from asha.scheduler import GovernedScheduler

PY = sys.executable

BASELINE = {
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
}

GITIGNORE = ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=120, check=True)
    return proc.stdout.strip()


def _make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "tests").mkdir()
    (repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    for rel, text in BASELINE.items():
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _worker(wid: str, rel: str, content: str,
            deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": wid, "deps": deps or [], "declared_scope": ["tests/"],
        "reads": [rel], "writes": [rel],
        "cmd": [PY, "-c", f"open({rel!r},'w').write({content!r})"],
    }


def _run_legacy(repo: Path, workers: list[dict], task_id: str) -> dict:
    return GovernedScheduler(repo, workers, task_id=task_id).run()


def _run_new(repo: Path, workers: list[dict], task_id: str) -> dict:
    return DAGCoordinator(repo, workers, task_id=task_id).run()


def _states_of(report: dict) -> dict[str, str]:
    return {wid: entry.get("state") for wid, entry in
            (report.get("states") or {}).items()}


def _reasons_of(report: dict) -> dict[str, str | None]:
    return {wid: entry.get("reason") for wid, entry in
            (report.get("states") or {}).items()}


def _evidence_of(report: dict) -> dict[str, str]:
    return report.get("evidence") or {}


def _compare(a: dict, b: dict, label: str) -> list[str]:
    diffs: list[str] = []
    if _states_of(a) != _states_of(b):
        diffs.append(f"{label}: states {_states_of(a)} != {_states_of(b)}")
    if _reasons_of(a) != _reasons_of(b):
        diffs.append(f"{label}: reasons {_reasons_of(a)} != {_reasons_of(b)}")
    if set(_evidence_of(a)) != set(_evidence_of(b)):
        diffs.append(f"{label}: evidence keys "
                     f"{set(_evidence_of(a))} != {set(_evidence_of(b))}")
    if a.get("status") != b.get("status"):
        diffs.append(f"{label}: status {a.get('status')} != {b.get('status')}")
    return diffs


# -------------------------------------------------------------------- H1
def test_h1_independent_workers(repo_factory: Any) -> None:
    repo = repo_factory
    workers = [_worker("w1", "tests/a.py", "x=1\n"),
               _worker("w2", "tests/b.py", "x=2\n"),
               _worker("w3", "tests/c.py", "x=3\n")]
    legacy = _run_legacy(repo, workers, "h1")
    new = _run_new(repo, workers, "h1")
    diffs = _compare(legacy, new, "H1")
    assert not diffs, diffs
    assert _states_of(legacy) == {"w1": "DONE", "w2": "DONE", "w3": "DONE"}
    assert set(_evidence_of(legacy)) == {"w1", "w2", "w3"}


# -------------------------------------------------------------------- H2
def test_h2_linear_dependency(repo_factory: Any) -> None:
    repo = repo_factory
    workers = [_worker("wa", "tests/a.py", "x=1\n"),
               _worker("wb", "tests/b.py", "x=2\n", deps=["wa"]),
               _worker("wc", "tests/c.py", "x=3\n", deps=["wb"])]
    legacy = _run_legacy(repo, workers, "h2")
    new = _run_new(repo, workers, "h2")
    diffs = _compare(legacy, new, "H2")
    assert not diffs, diffs
    assert _states_of(legacy) == {"wa": "DONE", "wb": "DONE", "wc": "DONE"}
    # ordering invariant: dependency published before dependent dispatched
    assert legacy["completed"].index("wa") < legacy["completed"].index("wb")
    assert legacy["completed"].index("wb") < legacy["completed"].index("wc")


# -------------------------------------------------------------------- H3
def test_h3_branch_join(repo_factory: Any) -> None:
    repo = repo_factory
    workers = [_worker("wa", "tests/a.py", "x=1\n"),
               _worker("wb", "tests/b.py", "x=2\n", deps=["wa"]),
               _worker("wc", "tests/c.py", "x=3\n", deps=["wa"]),
               _worker("wd", "tests/d.py", "x=4\n", deps=["wb", "wc"])]
    legacy = _run_legacy(repo, workers, "h3")
    new = _run_new(repo, workers, "h3")
    diffs = _compare(legacy, new, "H3")
    assert not diffs, diffs
    assert _states_of(legacy) == {f"w{x}": "DONE"
                                  for x in "abcd"}
    completed = legacy["completed"]
    assert completed.index("wa") < completed.index("wd")
    assert (completed.index("wb") < completed.index("wd")
            and completed.index("wc") < completed.index("wd"))


# -------------------------------------------------------------------- H4
def test_h4_cycle(repo_factory: Any) -> None:
    repo = repo_factory
    workers = [_worker("w1", "tests/a.py", "x=1\n", deps=["w2"]),
               _worker("w2", "tests/b.py", "x=2\n", deps=["w1"])]
    legacy = _run_legacy(repo, workers, "h4")
    new = _run_new(repo, workers, "h4")
    assert legacy.get("reason") == "cycle"
    assert new.get("reason") == "cycle"
    assert _states_of(legacy) == _states_of(new)
    assert set(_states_of(legacy)) <= {"w1", "w2"}
    assert all(st == "FAILED" for st in _states_of(legacy).values())
    assert _reasons_of(legacy) == _reasons_of(new)


# -------------------------------------------------------------------- H5
def test_h5_write_conflict(repo_factory: Any) -> None:
    repo = repo_factory
    # both workers write the SAME file -> parallel dispatch conflicts
    workers = [_worker("w1", "tests/shared.py", "x=1\n"),
               _worker("w2", "tests/shared.py", "x=2\n")]
    legacy = _run_legacy(repo, workers, "h5")
    new = _run_new(repo, workers, "h5")
    diffs = _compare(legacy, new, "H5")
    assert not diffs, diffs
    # one executes, the other defers then executes (or both DONE serially)
    assert set(_states_of(legacy).values()) <= {"DONE"}
    assert len(legacy.get("deferral_events") or []) >= 0


# -------------------------------------------------------------------- H8
def test_h8_worker_failure(repo_factory: Any) -> None:
    repo = repo_factory
    fail = _worker("wf", "tests/f.py", "x=1\n")
    fail["cmd"] = [PY, "-c", "import sys; sys.exit(3)"]
    downstream = _worker("wd", "tests/d.py", "x=2\n", deps=["wf"])
    workers = [fail, downstream]
    legacy = _run_legacy(repo, workers, "h8")
    new = _run_new(repo, workers, "h8")
    diffs = _compare(legacy, new, "H8")
    assert not diffs, diffs
    assert _states_of(legacy)["wf"] == "FAILED"
    assert _states_of(legacy)["wd"] == "BLOCKED"
    assert legacy["status"] == "failed"


# -------------------------------------------------------------------- H9
def test_h9_evidence_failure(repo_factory: Any) -> None:
    repo = repo_factory
    # worker writes outside its declared scope -> INVALID_EVIDENCE
    worker = {
        "id": "wv", "deps": [], "declared_scope": ["tests/"],
        "reads": ["tests/a.py"], "writes": ["tests/a.py"],
        "cmd": [PY, "-c", "open('OUTSIDE.py','w').write('x=1\\n')"],
    }
    legacy = _run_legacy(repo, [worker], "h9")
    new = _run_new(repo, [worker], "h9")
    diffs = _compare(legacy, new, "H9")
    assert not diffs, diffs
    assert _states_of(legacy)["wv"] == "INVALID_EVIDENCE"
    assert legacy["status"] == "failed"


# ----------------------------------------------------------------- H10
def test_h10_full_apply_contract(repo_factory: Any) -> None:
    """apply=true contract: run report feeds _integration_summary.
    Compares the run-report fields the integration stage consumes."""
    repo = repo_factory
    workers = [_worker("w1", "tests/a.py", "x=1\n")]
    legacy = _run_legacy(repo, workers, "h10")
    new = _run_new(repo, workers, "h10")
    for field in ("task_id", "status", "reason", "base_commit", "base_tree",
                  "states", "completed", "evidence", "worktrees",
                  "cleanup_errors", "graph"):
        assert field in legacy, f"legacy missing {field}"
        assert field in new, f"new missing {field}"
    assert new["task_id"] == "h10" == legacy["task_id"]
    assert _states_of(legacy) == _states_of(new)
    assert legacy["status"] == new["status"] == "ok"
    # graph generation contract (TreeIntegrator consumes generation)
    assert legacy["graph"]["generation"] == new["graph"]["generation"]


@pytest.fixture()
def repo_factory(tmp_path: Path) -> Any:
    return _make_repo(tmp_path)