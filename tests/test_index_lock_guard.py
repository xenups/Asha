"""A locked git index: wait for a live owner, never delete its lock.

The incident reported `fatal: Unable to create '.git/worktrees/w1/index.lock'`
for a lock another git process owned. Asha now waits briefly (a worker's own
git command usually releases it), and beyond that refuses with the exact file
and a safe recovery -- it has no way to prove the owner is gone, so it never
removes the file itself.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from asha import worktree
from asha.types import OrchestratorError


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("base\n")
    for args in (["git", "init", "-q", str(tmp_path)],
                 ["git", "-C", str(tmp_path), "add", "-A"]):
        subprocess.run(args, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "base"],
                   check=True, capture_output=True)
    return tmp_path


def test_a_live_lock_is_reported_never_deleted(tmp_path: Path,
                                               monkeypatch) -> None:
    monkeypatch.setattr(worktree, "INDEX_LOCK_WAIT_S", 0.6)
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("changed\n")
    lock = worktree._index_lock_path(repo)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")                      # an owner we cannot see
    with pytest.raises(OrchestratorError) as caught:
        worktree._commit_all(repo, "run")
    message = str(caught.value)
    assert "git index is locked" in message
    assert str(lock) in message
    assert "never deletes" in message
    assert lock.exists(), "Asha must not remove a lock it cannot prove stale"
    head = subprocess.run(["git", "-C", str(repo), "log", "--oneline"],
                          capture_output=True, text=True).stdout
    assert len(head.strip().splitlines()) == 1, "no commit may be created"


def test_a_transient_lock_is_waited_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(worktree, "INDEX_LOCK_WAIT_S", 5.0)
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("changed\n")
    lock = worktree._index_lock_path(repo)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")

    def release() -> None:
        time.sleep(0.4)
        lock.unlink()

    released = threading.Thread(target=release)
    released.start()
    try:
        worktree._commit_all(repo, "run")
    finally:
        released.join()
    files = subprocess.run(["git", "-C", str(repo), "show", "--stat",
                            "--oneline", "HEAD"], capture_output=True,
                           text=True).stdout
    assert "a.txt" in files


def test_an_unlocked_index_commits_normally(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "b.txt").write_text("new\n")
    worktree._commit_all(repo, "run")
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"],
                             capture_output=True, text=True).stdout
    assert "b.txt" in tracked
