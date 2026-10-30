"""D-4: a run_spec / DAG run writes a reconstructable, correlated journal."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha import mcp_server, telemetry  # noqa: E402
from asha.common import paths as common_paths  # noqa: E402
from asha.governance import dag  # noqa: E402

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


def _worker(wid: str, *, deps: list[str] | None = None,
            cmd: str = "printf ok > out.txt") -> dict:
    return {"id": wid, "deps": list(deps or []),
            "declared_scope": ["out.txt"], "reads": [], "writes": ["out.txt"],
            "cmd": ["sh", "-c", cmd]}


def _journal_events(repo: Path, task_id: str) -> list[dict]:
    path = common_paths.get_journal_dir(repo) / (task_id + ".jsonl")
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(repo: Path, workers: list[dict], task_id: str) -> tuple[dict, list[dict]]:
    journal = mcp_server._open_dag_journal(repo, task_id, workers)
    try:
        report = dag.run_workers_dag(repo, workers, task_id=task_id,
                                     journal=journal)
    finally:
        mcp_server._close_dag_journal(journal, task_id, ok=None)
    return report, _journal_events(repo, task_id)


def test_single_worker_run_is_reconstructable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report, events = _run(repo, [_worker("w1")], "task-single")
    assert report["status"] == "ok"
    assert [event["event_type"] for event in events] == [
        "execution_started", "execution_started", "sealed", "sealed"]
    assert all(event["run_id"] == "task-single" for event in events)
    worker_events = [event for event in events
                     if event["payload"].get("worker_id")]
    execution_id = report["states"]["w1"]["execution_id"]
    for event in worker_events:
        assert event["payload"]["task_id"] == "task-single"
        assert event["payload"]["worker_id"] == "w1"
        assert event["payload"]["execution_id"] == execution_id
    terminal = events[-1]["payload"]
    assert terminal["scope"] == "task" and terminal["status"] == "ok"
    assert terminal["worker_states"] == {"w1": "DONE"}


def test_two_concurrent_workers_have_independent_ids(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    workers = [_worker("wa", cmd="printf x > out.txt"),
               _worker("wb", cmd="printf y > out.txt")]
    report, events = _run(repo, workers, "task-two")
    ids = {wid: report["states"][wid]["execution_id"] for wid in ("wa", "wb")}
    assert len(set(ids.values())) == 2
    starts = {event["payload"]["worker_id"]: event["payload"]["execution_id"]
              for event in events
              if event["event_type"] == "execution_started"
              and event["payload"].get("worker_id")}
    assert starts == ids
    assert events[-1]["payload"]["scope"] == "task"
    assert events[-1]["event_type"] == "sealed"


def test_one_failing_worker_is_local_and_task_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    workers = [_worker("wa", cmd="printf ok > out.txt"),
               _worker("wb", cmd="printf bad > out.txt; exit 4")]
    report, events = _run(repo, workers, "task-mixed")
    assert report["states"]["wa"]["state"] == "DONE"
    assert report["states"]["wb"]["state"] == "FAILED"
    per_worker = [(event["payload"]["worker_id"], event["event_type"])
                  for event in events
                  if event["payload"].get("worker_id")
                  and event["event_type"] in ("sealed", "execution_failed")]
    assert ("wa", "sealed") in per_worker
    assert ("wb", "execution_failed") in per_worker
    assert events[-1]["event_type"] == "execution_failed"
    assert events[-1]["payload"]["worker_states"] == {
        "wa": "DONE", "wb": "FAILED"}


def test_all_workers_failing_terminates_the_task_as_failed(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    workers = [_worker("wa", cmd="exit 9"), _worker("wb", cmd="exit 8")]
    report, events = _run(repo, workers, "task-allfail")
    assert report["status"] == "failed"
    assert events[-1]["event_type"] == "execution_failed"
    assert events[-1]["payload"]["scope"] == "task"


def test_blocked_worker_gets_no_execution_events(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    workers = [_worker("wa", cmd="exit 5"),
               _worker("wb", deps=["wa"], cmd="printf y > out.txt")]
    report, events = _run(repo, workers, "task-blocked")
    assert report["states"]["wb"]["state"] == "BLOCKED"
    for wid in [event["payload"].get("worker_id") for event in events]:
        assert wid != "wb"
    assert events[-1]["payload"]["worker_states"]["wb"] == "BLOCKED"


def test_task_terminal_is_never_written_before_the_workers(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    workers = [_worker("wa"), _worker("wb", deps=["wa"])]
    _, events = _run(repo, workers, "task-order")
    task_events = [index for index, event in enumerate(events)
                   if event["payload"].get("scope") == "task"]
    assert task_events == [len(events) - 1]


def test_broken_journal_fails_the_run_instead_of_faking_success(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    class Broken:
        def append(self, *args: object, **kwargs: object) -> None:
            raise OSError("journal write failed")

        def close(self) -> None:
            return None

    try:
        dag.run_workers_dag(repo, [_worker("w1")], task_id="task-broken",
                            journal=Broken())
    except OSError as exc:
        assert "journal write failed" in str(exc)
    else:
        raise AssertionError("a broken journal must not be a successful run")
    assert not list(common_paths.get_journal_dir(repo).glob("task-broken*"))


def test_concurrent_appends_keep_records_whole(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run_id = "task-" + uuid.uuid4().hex[:6]
    writer = telemetry.EventJournalWriter(repo, run_id)

    def emit(worker: int) -> None:
        for _ in range(25):
            writer.append("execution_started",
                          telemetry.with_correlation(
                              {"worker_id": "w%d" % worker},
                              task_id=run_id, worker_id="w%d" % worker),
                          critical=True)

    threads = [threading.Thread(target=emit, args=(index,))
               for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    writer.close()

    events = _journal_events(repo, run_id)
    assert len(events) == 100
    assert all(event["payload"]["worker_id"].startswith("w") for event in events)
    _, integrity = telemetry.read_events(
        (common_paths.get_journal_dir(repo) / (run_id + ".jsonl"))
        .read_text(encoding="utf-8"))
    assert integrity.invalid_trailing is False
    assert integrity.record_count == 100


def test_run_spec_handler_writes_the_run_journal(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = {"workers": [_worker("wa"), _worker("wb", deps=["wa"])]}
    result = mcp_server.asha_run_spec(
        {"root": str(repo), "spec_content": spec, "apply": True},
        task_id_factory=lambda: "mcp-dagdjournal",
        execution_id_factory=lambda: "exec-dagdjournal")
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["task_id"] == "mcp-dagdjournal"
    events = _journal_events(repo, "mcp-dagdjournal")
    assert events[0]["event_type"] == "execution_started"
    assert events[0]["payload"]["execution_mode"] == "dag"
    assert events[-1]["payload"]["scope"] == "task"
    kinds = [event["event_type"] for event in events]
    assert kinds.count("sealed") >= 2
