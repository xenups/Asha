"""Phase 6 -- Section 9 coalescing is a fact about the worker, not a stopwatch.

Two completions whose regions overlap must publish ONE generation. The old
implementation decided "overlap" with ``wait(futures, timeout=0.5)`` after the
first completion woke the loop, so a sibling whose evidence seal took longer
than half a second was pushed into a second pass (a second generation for one
overlapping region). These tests create that lag on purpose and also prove the
correction did not swing the other way -- a still-executing sibling must never
block a publication.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asha

PY = sys.executable

GITIGNORE = (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
             ".mypy_cache/\n.ruff_cache/\n")
BASELINE = {
    ".gitignore": GITIGNORE,
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    "pkg/f.py": "VALUE = 1\n",
    "pkg/b.py": "VALUE = 1\n",
    "tests/fast.py": "X = 0\n",
    "tests/slow.py": "X = 0\n",
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


def _py(lines: list[str]) -> list[str]:
    return [PY, "-c", "; ".join(lines)]


def _w(wid: str, *, declared=("pkg/",), writes=(), cmd=None) -> dict:
    return {"id": wid, "deps": [], "declared_scope": list(declared),
            "reads": [], "writes": list(writes),
            "cmd": cmd or [PY, "-c", "pass"]}


def test_a_slow_seal_cannot_split_an_overlapping_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    barrier = threading.Barrier(2)
    workers = [
        _w("A", writes=["pkg/a.py"],
           cmd=_py(["from pathlib import Path",
                    ("Path('pkg/a.py').write_text("
                     "'import pkg.b\\n\\nMARK = pkg.b.VALUE\\n')")])),
        _w("B", writes=["pkg/b.py"],
           cmd=_py(["from pathlib import Path",
                    "Path('pkg/b.py').write_text('VALUE = 2\\n')"])),
    ]

    def execute(worker: dict, worktree: Path) -> int:
        proc = subprocess.run(worker["cmd"], cwd=worktree,
                              capture_output=True, text=True, timeout=60)
        try:
            barrier.wait(timeout=15)   # both commands finish together
        except threading.BrokenBarrierError:
            return 1
        return proc.returncode

    sched = asha.GovernedScheduler(repo, workers, task_id="p6-slow-seal",
                                   execute=execute)
    collect = sched._collect

    def slow_collect(worker, path, rc, tail, **kwargs):
        """Delay B's EVIDENCE SEAL (not its command) past the old window."""
        if worker["id"] == "B":
            time.sleep(1.5)
        return collect(worker, path, rc, tail, **kwargs)

    monkeypatch.setattr(sched, "_collect", slow_collect)
    report = sched.run()

    assert report["status"] == "ok", report
    assert len(sched.reconcile_log) == 1, sched.reconcile_log
    entry = sched.reconcile_log[0]
    assert set(entry["files"]) == {"pkg/a.py", "pkg/b.py"}
    assert entry["generation"] == 1
    # a failed pass never advances the generation it refused to publish
    generation = sched.graph.generation
    assert sched._reconcile_batch([
        ("pkg/duo.py", "D = 1\n", "A", "tree-a"),
        ("pkg/duo.py", "D = 2\n", "B", "tree-b")]) is False
    assert sched.graph.generation == generation


def test_a_running_sibling_never_blocks_a_publication(
        tmp_path: Path) -> None:
    """The correction must not wait for commands that are still executing."""
    repo = _make_repo(tmp_path)
    release = threading.Event()
    workers = [
        _w("SLOW", declared=["tests/"], writes=["tests/slow.py"],
           cmd=_py(["import time", "time.sleep(0.1)",
                    "from pathlib import Path",
                    "Path('tests/slow.py').write_text('X = 1\\n')"])),
        _w("FAST", declared=["tests/"], writes=["tests/fast.py"],
           cmd=_py(["from pathlib import Path",
                    "Path('tests/fast.py').write_text('Y = 1\\n')"])),
    ]

    def execute(worker: dict, worktree: Path) -> int:
        if worker["id"] == "SLOW":
            # runs only once the first publication already happened
            assert release.wait(timeout=30), "publication never happened"
        proc = subprocess.run(worker["cmd"], cwd=worktree,
                              capture_output=True, text=True, timeout=60)
        return proc.returncode

    sched = asha.GovernedScheduler(repo, workers, task_id="p6-running",
                                   execute=execute)
    reconcile_batch = sched._reconcile_batch

    def observed_batch(items):
        result = reconcile_batch(items)
        release.set()          # unblock SLOW only after real publication
        return result

    sched._reconcile_batch = observed_batch  # type: ignore[method-assign]
    outcome: dict = {}
    runner = threading.Thread(target=lambda: outcome.update(sched.run()))
    runner.start()
    runner.join(timeout=90)
    assert not runner.is_alive(), "publication blocked on a running worker"
    release.set()

    assert outcome["status"] == "ok", outcome
    assert set(sched.reconcile_log[0]["files"]) == {"tests/fast.py"}
    assert sched.state_of("SLOW") == "DONE"
    assert sched.state_of("FAST") == "DONE"
