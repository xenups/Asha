"""Phase 4: one concurrency-safe verification environment for every caller.

Every verification invocation gets its own environment dict built from the
current process environment plus cache redirections for its own repository, so
the tools cannot write into the target worktree and two concurrent runs cannot
see each other's cache paths. The checks here drive real subprocesses; the
scheduler and worker cases drive their real entry points, not the helper.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from asha import check_runner, execution, verification_env
from asha.common import paths as common_paths
from asha.governance import worker_execution

CACHE_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
ENV_KEYS = ("PYTHONDONTWRITEBYTECODE", "PYTEST_ADDOPTS", "MYPY_CACHE_DIR",
            "RUFF_CACHE_DIR")

BASELINE = {
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "pkg/__init__.py": "",
    "pkg/leaf.py": "VALUE = 1\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "fixture@example.com"),
                 ("config", "user.name", "Fixture"),
                 ("config", "commit.gpgsign", "false")):
        _git(repo, *args)
    for rel, text in BASELINE.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _resolved(files: list[str] | None = None) -> dict[str, Any]:
    return {"checks": ["ruff", "pytest", "mypy"],
            "affected_files": files if files is not None else ["pkg/leaf.py"],
            "base": None, "scope": "all"}


def _caches_in(repo: Path) -> list[str]:
    return sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*")
                  if p.is_dir() and p.name in CACHE_DIRS)


def _env_snapshot() -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in ENV_KEYS}


# ------------------------------------------------------------------ contract

def test_overrides_cover_every_producer_and_leave_the_process_alone(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    before = _env_snapshot()

    overrides = verification_env.env_overrides(repo)

    assert set(overrides) == set(ENV_KEYS)
    assert overrides["PYTHONDONTWRITEBYTECODE"] == "1"
    assert verification_env.PYTEST_DISABLE_CACHE in overrides["PYTEST_ADDOPTS"]
    cache = verification_env.cache_root(repo)
    assert overrides["MYPY_CACHE_DIR"] == str(cache / "mypy")
    assert overrides["RUFF_CACHE_DIR"] == str(cache / "ruff")
    assert (cache / "mypy").is_dir() and (cache / "ruff").is_dir()
    assert repo not in cache.parents
    assert _env_snapshot() == before


def test_caller_pytest_options_are_kept_and_not_duplicated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x --maxfail=1")

    first = verification_env.env_overrides(repo)["PYTEST_ADDOPTS"]
    second = verification_env.env_overrides(repo)["PYTEST_ADDOPTS"]

    assert first == second
    assert "-x --maxfail=1" in first
    assert first.count(verification_env.PYTEST_DISABLE_CACHE) == 1


def test_unpreparable_cache_root_is_reported_not_silently_ignored(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setattr(verification_env.common_paths, "get_cache_dir",
                        lambda root: blocker / "cache")

    with pytest.raises(verification_env.VerificationEnvError):
        verification_env.env_overrides(repo)


# ---------------------------------------------------------------- execution

def test_real_checks_use_the_overrides_and_keep_the_target_clean(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    before = _env_snapshot()

    entries = check_runner.run(
        repo, _resolved(), env_overrides=verification_env.env_overrides(repo))

    assert [(e["name"], e["status"]) for e in entries] == [
        ("ruff", "passed"), ("pytest", "passed"), ("mypy", "passed")]
    assert _caches_in(repo) == []
    assert _git(repo, "status", "--porcelain") == ""
    assert (verification_env.cache_root(repo) / "ruff").is_dir()
    assert _env_snapshot() == before


def test_run_scoped_honours_the_same_overrides(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    entries = check_runner.run_scoped(
        repo, changed_files=["pkg/leaf.py"], targeted_tests=["tests/test_ok.py"],
        mypy_targets=["pkg/leaf.py"],
        env_overrides=verification_env.env_overrides(repo))

    assert all(entry["status"] in ("passed", "skipped") for entry in entries)
    assert _caches_in(repo) == []


def test_concurrent_invocations_do_not_share_cache_paths(
        tmp_path: Path) -> None:
    repo_a = _make_repo(tmp_path, "repo-a")
    repo_b = _make_repo(tmp_path, "repo-b")
    before = _env_snapshot()

    def run(repo: Path) -> list[dict[str, Any]]:
        return check_runner.run(
            repo, _resolved(), env_overrides=verification_env.env_overrides(repo))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (repo_a, repo_b)))

    assert [[e["status"] for e in entries] for entries in results] == [
        ["passed"] * 3, ["passed"] * 3]
    for repo in (repo_a, repo_b):
        assert _caches_in(repo) == []
        assert _git(repo, "status", "--porcelain") == ""
    cache_a = verification_env.cache_root(repo_a)
    cache_b = verification_env.cache_root(repo_b)
    assert cache_a != cache_b
    assert repo_b not in cache_a.parents and repo_a not in cache_b.parents
    assert _env_snapshot() == before


# --------------------------------------------------------------- error paths

def test_failing_tool_keeps_its_exit_code_and_pollutes_nothing(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "tests/test_ok.py").write_text(
        "def test_ok():\n    assert False\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "break")

    entries = check_runner.run(
        repo, _resolved(), env_overrides=verification_env.env_overrides(repo))

    pytest_entry = next(e for e in entries if e["name"] == "pytest")
    assert pytest_entry["status"] == "failed"
    assert pytest_entry["exit_code"] == 1
    assert _caches_in(repo) == []


def test_timeout_classification_is_unchanged(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "tests/test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(4)\n",
        encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "slow")

    entries = check_runner.run_scoped(
        repo, changed_files=["tests/test_slow.py"],
        targeted_tests=["tests/test_slow.py"], mypy_targets=[],
        timeout=1, env_overrides=verification_env.env_overrides(repo))

    slow = next(e for e in entries if e["name"] == "pytest")
    assert slow["status"] == "failed"
    assert slow["note"] == "timed out after 1s"
    assert _caches_in(repo) == []


def test_a_broken_spawn_fails_loudly_and_leaves_the_environment_alone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A launch that blows up must never be reported as a clean run, and the
    failed invocation must not leave redirects behind in the process."""
    repo = _make_repo(tmp_path)
    before = _env_snapshot()

    def _no_launch(*args: object, **kwargs: object) -> execution.ExecutionOutcome:
        raise OSError("spawn refused")

    monkeypatch.setattr(check_runner, "spawn_capture", _no_launch)
    with pytest.raises(OSError):
        check_runner.run(repo, _resolved(),
                         env_overrides=verification_env.env_overrides(repo))

    assert _env_snapshot() == before
    assert _caches_in(repo) == []


# -------------------------------------------------------- real caller paths

class _Decision:
    """The only shape _run_checks reads: an ineligible scoping decision."""

    eligible = False
    targeted_tests: list[str] = []
    mypy_targets: list[str] = []


def test_worker_verification_path_isolates_its_caches(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    entries = worker_execution._run_checks(_Decision(), repo, _resolved())

    assert all(entry["status"] in ("passed", "skipped") for entry in entries)
    assert _caches_in(repo) == []
    assert (verification_env.cache_root(repo) / "ruff").is_dir()


def test_scheduler_verification_path_isolates_its_caches(
        tmp_path: Path) -> None:
    from asha.scheduler import GovernedScheduler

    repo = _make_repo(tmp_path)
    code = ("import pathlib;"
            "pathlib.Path('pkg/out.py').write_text('RESULT = 1\\n',"
            "encoding='utf-8')")
    worker = {
        "id": "worker_a",
        "declared_scope": ["pkg/out.py"],
        "reads": [],
        "writes": ["pkg/out.py"],
        "deps": [],
        "cmd": [sys.executable, "-c", code],
        "verify": "pytest tests/ -q",
    }
    sched = GovernedScheduler(repo, [worker], task_id="phase4",
                              keep_worktrees=True)
    worktree = sched.dispatcher.create("worker_a")
    try:
        result = sched._run_one(worker, worktree)
    finally:
        sched.dispatcher.remove("worker_a")

    assert result["state"] in ("COMPLETE", "DONE"), result
    assert _caches_in(worktree) == []
    assert (verification_env.cache_root(worktree) / "ruff").is_dir()
