"""Phase 7 -- FAST_PATH concurrency contract and its reach into the Phase 6 drain.

FAST_PATH is not a separate scheduler: it is the route that skips the isolated
worktree and runs the worker against the repository directly. ``_run_one_fast``
holds ``_fast_lock`` around the HEAD read *and* ``_run_one`` (command + evidence
seal), so two FAST_PATH commands are serialized by construction -- their
completion regions cannot overlap. What still has to hold on this route:

* route selection really happens (the flag alone is not proof),
* the command-completion registration and the coalescing drain the normal route
  uses are reached here too,
* a FAST_PATH command that is still running never blocks a publication.

The overlapping-region coalescing invariant is covered on the route where
overlap is possible by tests/test_reconcile_coalescing.py; forcing it onto a
serialized route would be testing an ordering the implementation forbids.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha.scheduler import GovernedScheduler

PY = sys.executable

BASELINE = {
    ".gitignore": (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
                   ".mypy_cache/\n.ruff_cache/\n"),
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    "pkg/module_a.py": 'VALUE_A = "original a"\n',
    "pkg/module_b.py": 'VALUE_B = "original b"\n',
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True).stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
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


def _worker(wid: str, rel: str, marker: str) -> dict:
    """Disjoint single-file worker: classifies PROVEN_DISJOINT -> FAST_PATH."""
    return {
        "id": wid,
        "deps": [],
        "declared_scope": [rel],
        "reads": [rel],
        "writes": [rel],
        "cmd": [PY, "-c",
                (f"from pathlib import Path; "
                 f"Path({rel!r}).write_text({marker!r})")],
    }


def _pair() -> list[dict]:
    return [_worker("wa", "pkg/module_a.py", 'VALUE_A = "fast a"\n'),
            _worker("wb", "pkg/module_b.py", 'VALUE_B = "fast b"\n')]


def test_fast_route_selected_and_registers_command_completion(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sched = GovernedScheduler(repo, _pair(), task_id="p7-route",
                              fast_path_enabled=True)
    real = sched._run_one_fast
    entered: list[str] = []

    def traced(worker: dict) -> dict:
        entered.append(str(worker["id"]))
        return real(worker)          # the real route still executes

    sched._run_one_fast = traced      # type: ignore[method-assign]
    report = sched.run()

    assert report["status"] == "ok", (report["states"], report["reason"])
    # route activation, from the scheduler's own routing record
    routing = report.get("routing") or {}
    assert {entry["mode"] for entry in routing.values()} == {"fast_path"}, \
        "FAST_PATH was not selected"
    assert sorted(entered) == ["wa", "wb"]
    # no isolated worktree: this really was the FAST_PATH lifecycle
    assert report["worktrees"] == {}
    assert sched.dispatcher.paths == {}
    # the shared Phase 6 registration ran on this route
    assert sched._command_finished == {"wa", "wb"}
    # one publication per generation step; union of the observed regions
    published = [entry for entry in sched.reconcile_log if entry["ok"]]
    assert published, sched.reconcile_log
    assert [entry["generation"] for entry in published] == list(
        range(1, len(published) + 1))
    assert set().union(*(set(entry["files"]) for entry in published)) == {
        "pkg/module_a.py", "pkg/module_b.py"}


def test_fast_route_serializes_commands(tmp_path: Path) -> None:
    """``_fast_lock`` covers command + seal, so no second command starts first."""
    repo = _make_repo(tmp_path)
    first_finished = threading.Event()
    overlap: list[str] = []

    def execute(worker: dict, worktree: Path) -> int:
        wid = str(worker["id"])
        if wid == "wb" and not first_finished.is_set():
            overlap.append(wid)
        proc = subprocess.run(worker["cmd"], cwd=worktree,
                              capture_output=True, text=True, timeout=60)
        if wid == "wa":
            # widens the window so a missing lock would be observed; the
            # verdict comes from the ordering, not from this delay
            time.sleep(0.2)
            first_finished.set()
        return proc.returncode

    sched = GovernedScheduler(repo, _pair(), task_id="p7-serialized",
                              fast_path_enabled=True, execute=execute)
    report = sched.run()

    assert overlap == [], "second FAST_PATH command started before the first ended"
    assert report["status"] == "ok", (report["states"], report["reason"])
    assert sched.state_of("wa") == "DONE"
    assert sched.state_of("wb") == "DONE"
    # the lock left nothing behind: both commands are recorded exactly once
    assert sched._command_finished == {"wa", "wb"}


def test_a_running_fast_sibling_does_not_block_publication(
        tmp_path: Path) -> None:
    """Publication must not await a FAST_PATH command that is still running."""
    repo = _make_repo(tmp_path)
    second_started = threading.Event()
    published = threading.Event()

    def execute(worker: dict, worktree: Path) -> int:
        if str(worker["id"]) == "wb":
            second_started.set()
            # only a publication of wa can release this command
            assert published.wait(timeout=30), \
                "publication waited for a running FAST_PATH command"
        proc = subprocess.run(worker["cmd"], cwd=worktree,
                              capture_output=True, text=True, timeout=60)
        return proc.returncode

    sched = GovernedScheduler(repo, _pair(), task_id="p7-nonblocking",
                              fast_path_enabled=True, execute=execute)
    real_batch = sched._reconcile_batch

    def observed_batch(items):
        # deterministic ordering: wa publishes only while wb is executing
        assert second_started.wait(timeout=30), "wb never started"
        result = real_batch(items)
        if result:
            published.set()
        return result

    sched._reconcile_batch = observed_batch  # type: ignore[method-assign]
    report = sched.run()

    assert published.is_set()
    assert report["status"] == "ok", (report["states"], report["reason"])
    assert set(sched.reconcile_log[0]["files"]) == {"pkg/module_a.py"}
    assert sched.state_of("wb") == "DONE"
