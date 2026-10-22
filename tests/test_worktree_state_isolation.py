"""Follow-up A: linked worktrees keep ordinary state apart and share only the
verify-tip evidence store (``--stack`` reads it from the main checkout).

The fixtures use real ``git worktree`` checkouts, so ``.git`` is a ``gitdir:``
marker file and ``_shared_root()`` parses real git metadata instead of a path
that merely looks like a repository.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import asha.mcp_server as server
from asha import evidence
from asha.common import paths

PY = sys.executable


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _commit(repo: Path, rel: str, text: str, message: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "main-repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _commit(repo, "app.py", "VALUE = 1\n", "baseline")
    _commit(repo, "tests/test_ok.py",
            "def test_ok():\n    assert True\n", "test file")
    return repo


def _linked(repo: Path, tmp_path: Path) -> Path:
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "linked-branch", str(linked))
    return linked


def test_linked_worktree_state_is_local_and_evidence_is_shared(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = _linked(repo, tmp_path)

    # the real linked-worktree metadata path, not a lookalike directory
    assert (linked / ".git").is_file()
    assert (linked / ".git").read_text().startswith("gitdir:")

    assert paths.get_state_dir(repo) != paths.get_state_dir(linked)
    assert paths.get_verify_tip_dir(repo) == paths.get_verify_tip_dir(linked)
    # telemetry, lock, journal, orchestrator, cache all follow the checkout
    for name in ("mcp_live_telemetry.jsonl", "lock"):
        assert (paths.get_state_dir(repo) / name
                != paths.get_state_dir(linked) / name)
    assert paths.get_journal_dir(repo) != paths.get_journal_dir(linked)
    assert (paths.get_orchestrator_dir(repo)
            != paths.get_orchestrator_dir(linked))
    assert paths.get_cache_dir(repo) != paths.get_cache_dir(linked)


def test_seal_written_in_a_worktree_is_read_from_the_main_checkout(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = _linked(repo, tmp_path)
    _commit(linked, "app.py", "VALUE = 2\n", "linked work")
    head = _git(linked, "rev-parse", "HEAD").strip()
    tree = _git(linked, "rev-parse", "HEAD^{tree}").strip()

    result = server.asha_verify_tip({
        "root": str(linked), "base_ref": "main",
        "acceptance_command": f"{PY} -m pytest tests -q"})

    assert not result.get("isError"), result
    shared = paths.get_verify_tip_dir(repo)
    assert (shared / f"{head[:12]}-{tree[:12]}.evidence.json").is_file()
    assert server._tip_evidence_state(
        paths.get_state_dir(repo), head, tree,
        verify_tip_dir=shared) == "verified"


def test_same_commit_seals_are_keyed_by_tree_not_overwritten(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    shared = paths.get_verify_tip_dir(repo)
    foreign_tree = "f" * 40
    evidence.write_to(
        shared / f"{head[:12]}-{foreign_tree[:12]}.evidence.json",
        evidence.seal({
            "schema": evidence.SCHEMA_VALIDATED,
            "stage": "verify_tip",
            "kind": "verification",
            "scope": "S2",
            "checks": [],
            "commit": head,
            "tree_hash": foreign_tree,
            "snapshot_id": "f" * 64,
            "observed_at": evidence.now_iso(),
            "validation": {"status": "PASS", "policy": "explicit",
                           "rule": "seeded"},
            "validation_status": "PASS",
            "ready_to_push": True,
            "authorized_to_ship": False,
        }))

    assert not server.asha_verify_tip({
        "root": str(repo), "base_ref": "main",
        "acceptance_command": f"{PY} -m pytest tests -q"}).get("isError")

    names = sorted(path.name for path in shared.glob(f"{head[:12]}-*.json"))
    assert names == [f"{head[:12]}-{tree[:12]}.evidence.json",
                     f"{head[:12]}-{foreign_tree[:12]}.evidence.json"]
    # each tree's lookup resolves to its own seal; neither sees a mismatch
    assert server._tip_evidence_state(
        paths.get_state_dir(repo), head, tree, verify_tip_dir=shared
    ) == "verified"
    assert server._tip_evidence_state(
        paths.get_state_dir(repo), head, foreign_tree, verify_tip_dir=shared
    ) == "verified"
