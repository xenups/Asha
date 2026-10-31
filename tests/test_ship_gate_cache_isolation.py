"""Operational regression: the Ship Gate must not write caches into the target.

Why this exists
---------------
``gate_ship`` runs ruff / pytest / mypy with the working directory set to the
reviewed repository. Python then writes ``__pycache__`` next to the imported
modules, so the NEXT finalize refused with::

    working tree is dirty; ship evidence requires a clean tree: ?? __pycache__/

A repository could be reviewed exactly once and then never again without a
manual cleanup -- and the user had made no change at all. These tests drive the
real gate (no monkeypatched ``check_runner``) and inspect the filesystem, since
an *ignored* cache directory is still physically inside the target worktree.

The redirect itself lives in ``asha.verification_env`` and is handed to the
check run as a per-invocation environment; what is asserted here is the
behaviour a repository owner cares about: caches land outside, the worktree
stays clean, repeat runs are allowed, and genuine source changes still refuse.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from asha import evidence
from asha.common import paths as common_paths
from asha.governance import semantic_review, ship_gate

CACHE_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")

# Deliberately no cache entries in .gitignore: the point is that the gate does
# not need the repository to hide its own verification debris.
BASELINE = {
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "pkg/__init__.py": "",
    "pkg/leaf.py": "VALUE = 1\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in BASELINE.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _reviewer(root: Path, snapshot_id: str, timeout_s: int) -> dict:
    """A valid phase2 semantic-review artifact for a clean review."""
    return {
        "schema": semantic_review.REVIEW_SCHEMA,
        "snapshot_id": snapshot_id,
        "policy_version": ship_gate.POLICY_VERSION,
        "status": "ok",
        "findings": [],
    }


def _caches_in(repo: Path) -> list[str]:
    """Every cache directory physically present inside the worktree."""
    return sorted(path.relative_to(repo).as_posix()
                  for path in repo.rglob("*")
                  if path.is_dir() and path.name in CACHE_DIRS)


def _cache_root(repo: Path) -> Path:
    return common_paths.get_cache_dir(repo) / "check-tools"


# ------------------------------------------------------------------ isolation

def test_gate_leaves_the_worktree_clean_and_caches_outside(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sealed = ship_gate.gate_ship(repo, reviewer=_reviewer)

    assert sealed["gate_state"] == "COMPLETED_CLEAN"
    source_checks = [check for check in sealed["checks"]
                     if check["name"] in ("ruff", "pytest", "mypy")]
    assert [check["name"] for check in source_checks] == [
        "ruff", "pytest", "mypy"], "the real checks must still run"
    # a justified skip (nothing to analyse) is still a clean gate; nothing
    # here may be a failed check
    assert all(check["status"] in ("passed", "skipped")
               for check in sealed["checks"])
    assert _caches_in(repo) == []
    assert _git(repo, "status", "--porcelain") == ""
    cache_root = _cache_root(repo)
    assert cache_root.is_dir() and repo not in cache_root.parents
    # ruff always runs; mypy is skipped when the target has nothing to analyse
    assert (cache_root / "ruff").is_dir()


def test_second_gate_run_is_not_blocked_by_the_first(tmp_path: Path) -> None:
    """The whole point: two real gate runs, no manual cleanup in between."""
    repo = _make_repo(tmp_path)
    first = ship_gate.gate_ship(repo, reviewer=_reviewer)
    second = ship_gate.gate_ship(repo, reviewer=_reviewer)

    assert first["gate_state"] == second["gate_state"] == "COMPLETED_CLEAN"
    assert second["authorized_to_ship"] is True
    assert _caches_in(repo) == []
    assert _git(repo, "status", "--porcelain") == ""


# --------------------------------------------- genuine changes still fail closed

def test_tracked_edit_after_the_gate_still_refuses(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ship_gate.gate_ship(repo, reviewer=_reviewer)
    _write(repo, "pkg/leaf.py", "VALUE = 2\n")

    with pytest.raises(evidence.EvidenceError, match="working tree is dirty"):
        ship_gate.gate_ship(repo, reviewer=_reviewer)


def test_new_untracked_source_file_still_refuses(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ship_gate.gate_ship(repo, reviewer=_reviewer)
    _write(repo, "pkg/extra.py", "EXTRA = 1\n")

    with pytest.raises(evidence.EvidenceError, match="working tree is dirty"):
        ship_gate.gate_ship(repo, reviewer=_reviewer)


def test_deleted_tracked_file_still_refuses(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ship_gate.gate_ship(repo, reviewer=_reviewer)
    (repo / "pkg/leaf.py").unlink()

    with pytest.raises(evidence.EvidenceError, match="working tree is dirty"):
        ship_gate.gate_ship(repo, reviewer=_reviewer)


# ------------------------------------------------------- failing gate stays red

def test_failing_checks_refuse_and_still_leave_no_caches(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "tests/test_ok.py",
           "def test_ok():\n    assert False\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "break the test")

    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo, reviewer=_reviewer)

    assert "EXECUTION_FAILED" in str(exc.value)
    sealed = common_paths.get_evidence_dir(repo) / "evidence.json"
    payload = sealed.read_text(encoding="utf-8")
    assert '"authorized_to_ship": false' in payload
    # isolation must not hide the failure, and must not be a side effect of it
    assert _caches_in(repo) == []
    assert _git(repo, "status", "--porcelain") == ""


# ------------------------------------------------------- environment hygiene

def test_the_redirect_restores_the_process_environment(tmp_path: Path) -> None:
    import os

    repo = _make_repo(tmp_path)
    before = {name: os.environ.get(name) for name in (
        "PYTHONDONTWRITEBYTECODE", "PYTEST_ADDOPTS", "MYPY_CACHE_DIR",
        "RUFF_CACHE_DIR")}
    ship_gate.gate_ship(repo, reviewer=_reviewer)
    after = {name: os.environ.get(name) for name in before}

    assert after == before
