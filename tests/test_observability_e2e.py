"""End-to-end: a real governed DAG run drives the real event stream."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha import mcp_server  # noqa: E402
from asha.governance import dag  # noqa: E402
from asha.observability import events as obs  # noqa: E402
from asha.observability import store  # noqa: E402

FILES = {"a.txt": "one\n", "test_ok.py": "def test_ok():\n    assert True\n"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@example.invalid")
    _git(repo, "config", "user.name", "A")
    for rel, text in FILES.items():
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _worker(wid: str, *, deps: list[str] | None = None,
            cmd: str = "printf ok > out.txt") -> dict:
    return {"id": wid, "deps": list(deps or []),
            "declared_scope": ["out.txt"], "reads": [], "writes": ["out.txt"],
            "cmd": ["sh", "-c", cmd]}


def _run(repo: Path, workers: list[dict], task_id: str) -> dict:
    journal = mcp_server._open_dag_journal(repo, task_id, workers)
    try:
        return dag.run_workers_dag(repo, workers, task_id=task_id,
                                   journal=journal)
    finally:
        mcp_server._close_dag_journal(journal, task_id, ok=None)


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASHA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(obs.ENV_ENABLE, "1")
    obs.reset()
    yield
    obs.reset()


def _events(repo: Path) -> list[dict]:
    runs = store.list_runs(repo)
    assert runs, "no run was recorded"
    return store.read_events(repo, runs[0]["run_id"])


def test_real_run_emits_the_whole_observable_lifecycle(tmp_path) -> None:
    repo = _repo(tmp_path, "repo-on")
    report = _run(repo, [_worker("w1"), _worker("w2", deps=["w1"])],
                  "task-e2e")
    assert report["status"] == "ok"
    rows = _events(repo)
    kinds = [r["event_type"] for r in rows]
    for required in ("run.started", "dag.created", "dag.validated",
                     "worker.started", "worktree.create.started",
                     "worktree.create.completed", "scope_validation.started",
                     "scope_validation.completed", "candidate_check.completed",
                     "delta.computed", "verdict.computed", "evidence.created",
                     "evidence.sealed", "run.completed"):
        assert required in kinds, f"missing {required}: {kinds}"

    dag_event = next(r for r in rows if r["event_type"] == "dag.created")
    assert {n["id"] for n in dag_event["data"]["nodes"]} == {"w1", "w2"}
    assert dag_event["data"]["edges"]["w2"] == ["w1"]
    assert [r["sequence"] for r in rows] == list(range(1, len(rows) + 1))

    snap = store.snapshot(repo, rows[0]["run_id"])
    assert snap["status"] == "ok"
    assert snap["workers"]["w1"]["state"] == "DONE"
    assert snap["workers"]["w2"]["state"] == "DONE"
    assert {p["id"] for p in snap["phases"]} >= {"dag", "worker", "scope",
                                                 "candidate", "delta",
                                                 "evidence"}


def test_failing_worker_is_reported_as_failed_not_running(tmp_path) -> None:
    repo = _repo(tmp_path, "repo-fail")
    report = _run(repo, [_worker("w1", cmd="printf half > out.txt; exit 3")],
                  "task-fail")
    assert report["status"] == "failed"
    rows = _events(repo)
    kinds = [r["event_type"] for r in rows]
    assert "worker.failed" in kinds
    assert kinds[-1] == "run.failed"
    snap = store.snapshot(repo, rows[0]["run_id"])
    assert snap["status"] == "failed"
    assert snap["workers"]["w1"]["state"] in ("FAILED", "INVALID_EVIDENCE")


def test_telemetry_does_not_change_the_governance_result(tmp_path) -> None:
    on = _repo(tmp_path, "repo-with")
    off = _repo(tmp_path, "repo-without")
    report_on = _run(on, [_worker("w1")], "task-on")
    obs.reset()
    import os
    os.environ.pop(obs.ENV_ENABLE, None)
    report_off = _run(off, [_worker("w1")], "task-off")

    def view(report: dict) -> dict:
        return {"status": report["status"],
                "states": {wid: entry["state"]
                           for wid, entry in report["states"].items()},
                "evidence": sorted(report["evidence"]),
                "cleanup_errors": report["cleanup_errors"]}

    assert view(report_on) == view(report_off)
    assert report_on["status"] == "ok"
    # telemetry never writes into the repository or the evidence document
    assert not (on / "observability").exists()
    evidence = next(Path(path).read_text("utf-8")
                    for path in report_on["evidence"].values())
    assert "event_type" not in evidence
    assert json.loads(evidence)["evidence_sha256"]
