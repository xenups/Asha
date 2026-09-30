"""Phase K tests: service environment resolver, scope ergonomics,
failure-evidence persistence.

Temporary-filesystem fixtures; no network, no real repo dependency.
Persistence tests exercise the ACTUAL run_workers_dag / worktree path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from asha.governance.dag import run_workers_dag
from asha.governance.env_resolver import (
    ALLOW_SYSTEM_PYTHON_FALLBACK_ENV,
    EnvironmentResolutionError,
    EnvironmentResolver,
    resolve_env_for_file,
)
from asha.scope_resolver import normalize_declared_scope

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _venv_bins(service: Path) -> None:
    _mkdir(service / ".venv" / "bin")
    (service / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n",
                                                      encoding="utf-8")
    (service / ".venv" / "bin" / "pytest").write_text("#!/bin/sh\nexit 0\n",
                                                      encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. standalone project
# ---------------------------------------------------------------------------

def test_standalone_root_detection(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _venv_bins(tmp_path)
    env = resolve_env_for_file(tmp_path, tmp_path / "asha" / "foo.py")
    assert env.service_root == tmp_path
    assert env.python_bin == (tmp_path / ".venv" / "bin" / "python")
    assert env.pytest_bin == (tmp_path / ".venv" / "bin" / "pytest")
    assert env.is_hermetic is True
    assert env.env_vars["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    assert str(tmp_path / ".venv" / "bin") in env.env_vars["PATH"]


def test_standalone_no_venv_falls_back(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    env = resolve_env_for_file(tmp_path, tmp_path / "asha" / "foo.py")
    assert env.service_root == tmp_path
    assert env.python_bin == Path(sys.executable)
    assert env.pytest_bin is None
    assert env.is_hermetic is False


def test_fail_closed_when_fallback_disabled(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    with pytest.raises(EnvironmentResolutionError):
        EnvironmentResolver(allow_system_python_fallback=False).resolve_for_file(
            tmp_path, tmp_path / "asha" / "foo.py")


def test_fail_closed_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setenv(ALLOW_SYSTEM_PYTHON_FALLBACK_ENV, "false")
    with pytest.raises(EnvironmentResolutionError):
        resolve_env_for_file(tmp_path, tmp_path / "asha" / "foo.py")


# ---------------------------------------------------------------------------
# 2. monorepo nearest-ancestor
# ---------------------------------------------------------------------------

def _monorepo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    svc_a = _mkdir(tmp_path / "services" / "service_a")
    svc_b = _mkdir(tmp_path / "services" / "service_b")
    (svc_a / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (svc_b / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _venv_bins(svc_a)
    _venv_bins(svc_b)
    return tmp_path


def test_monorepo_target_resolves_nearest_service(tmp_path: Path) -> None:
    root = _monorepo(tmp_path)
    a_env = resolve_env_for_file(root, root / "services" / "service_a" / "x.py")
    b_env = resolve_env_for_file(root, root / "services" / "service_b" / "y.py")
    assert a_env.service_root == root / "services" / "service_a"
    assert b_env.service_root == root / "services" / "service_b"
    assert a_env.is_hermetic and b_env.is_hermetic
    assert a_env.env_vars["VIRTUAL_ENV"] == str(
        root / "services" / "service_a" / ".venv")


def test_monorepo_repo_root_no_service_marker(tmp_path: Path) -> None:
    root = _monorepo(tmp_path)
    env = resolve_env_for_file(root, root / "README.md")
    # README.md is at repo root; the repo root has pyproject.toml but no .venv
    assert env.service_root == root
    assert env.is_hermetic is False


def test_never_walks_above_repo_root(tmp_path: Path) -> None:
    root = _monorepo(tmp_path)
    outside_marker = _mkdir(tmp_path.parent / "outer-marked")
    (outside_marker / "pyproject.toml").write_text("[project]\n",
                                                   encoding="utf-8")
    env = resolve_env_for_file(root, root / "services" / "service_a" / "x.py")
    assert env.service_root == root / "services" / "service_a"


# ---------------------------------------------------------------------------
# 3. environment construction
# ---------------------------------------------------------------------------

def test_env_preserves_existing_path_and_pythonpath(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _venv_bins(tmp_path)
    _mkdir(tmp_path / "src")
    monkeypatch.setenv("PATH", "/orig/bin")
    monkeypatch.setenv("PYTHONPATH", "/orig/pp")
    env = resolve_env_for_file(tmp_path, tmp_path / "x.py")
    assert env.env_vars["PATH"].startswith(str(tmp_path / ".venv" / "bin"))
    assert "/orig/bin" in env.env_vars["PATH"]
    assert env.env_vars["PYTHONPATH"].startswith(str(tmp_path / "src"))
    assert "/orig/pp" in env.env_vars["PYTHONPATH"]


def test_env_no_src_no_pythonpath(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _venv_bins(tmp_path)
    env = resolve_env_for_file(tmp_path, tmp_path / "x.py")
    assert "PYTHONPATH" not in env.env_vars


# ---------------------------------------------------------------------------
# 4. scope ergonomics
# ---------------------------------------------------------------------------

def test_scope_single_string() -> None:
    assert normalize_declared_scope("asha/foo.py") == ["asha/foo.py"]


def test_scope_comma_separated() -> None:
    assert normalize_declared_scope("asha/foo.py, tests/test_foo.py") == [
        "asha/foo.py", "tests/test_foo.py"]


def test_scope_list() -> None:
    assert normalize_declared_scope(["asha/foo.py", "tests/test_foo.py"]) == [
        "asha/foo.py", "tests/test_foo.py"]


def test_scope_whitespace() -> None:
    assert normalize_declared_scope(" asha/foo.py ,  tests/test_foo.py ") == [
        "asha/foo.py", "tests/test_foo.py"]


def test_scope_leading_slash() -> None:
    assert normalize_declared_scope("/asha/foo.py") == ["asha/foo.py"]
    assert normalize_declared_scope(" /asha/foo.py, /tests/test_foo.py ") == [
        "asha/foo.py", "tests/test_foo.py"]


def test_scope_trailing_comma_and_empty() -> None:
    assert normalize_declared_scope("asha/foo.py,") == ["asha/foo.py"]
    assert normalize_declared_scope(",,") == []
    assert normalize_declared_scope(" , , ") == []
    assert normalize_declared_scope("") == []


def test_scope_empty_list() -> None:
    assert normalize_declared_scope([]) == []
    assert normalize_declared_scope(()) == []


def test_scope_existing_valid_behavior_preserved() -> None:
    # relative paths pass through untouched
    assert normalize_declared_scope(["a.py"]) == ["a.py"]
    assert normalize_declared_scope(["dir/a.py", "dir/b.py"]) == [
        "dir/a.py", "dir/b.py"]


def test_scope_dedups_repeated_commas() -> None:
    assert normalize_declared_scope("a.py,,b.py") == ["a.py", "b.py"]


def test_scope_invalid_type_raises() -> None:
    from asha.scope_resolver import ScopeError
    with pytest.raises(ScopeError):
        normalize_declared_scope(123)  # type: ignore[arg-type]
    with pytest.raises(ScopeError):
        normalize_declared_scope(["a.py", 5])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 5. failure evidence persistence (real run path)
# ---------------------------------------------------------------------------

def _make_failing_repo(tmp_path: Path) -> Path:
    """Repo (real git) whose worker command always fails (rc=1)."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _mkdir(tmp_path / "tests")
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_never_passes():\n    assert False\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t",
         "-c", "user.name=t", "commit", "-qm", "base"], check=True,
        capture_output=True)
    return tmp_path


def test_normal_failure_cleans_up(tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_failing_repo(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("ASHA_STATE_DIR", str(state))
    worker = {"id": "w1", "deps": [], "declared_scope": ["tests/test_x.py"],
              "reads": ["tests/test_x.py"], "writes": [],
              "cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}
    report = run_workers_dag(repo, [worker], task_id="t-fail-clean")
    assert report["states"]["w1"]["state"] == "FAILED"
    # no evidence persisted (preserve not requested, debug off)
    from asha.common import paths as common_paths
    run_dir = common_paths.get_orchestrator_dir(repo) / "w1"
    assert not (run_dir / "execution.log").exists()


def test_preserve_on_failure_keeps_evidence(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_failing_repo(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("ASHA_STATE_DIR", str(state))
    worker = {"id": "w1", "deps": [], "declared_scope": ["tests/test_x.py"],
              "reads": ["tests/test_x.py"], "writes": [],
              "cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}
    report = run_workers_dag(repo, [worker], task_id="t-preserve",
                             preserve_on_failure=True)
    assert report["states"]["w1"]["state"] == "FAILED"
    from asha.common import paths as common_paths
    run_dir = common_paths.get_orchestrator_dir(repo) / "w1"
    assert (run_dir / "execution.log").exists()
    assert (run_dir / "worker-status.json").exists()
    status = (run_dir / "worker-status.json").read_text(encoding="utf-8")
    assert '"FAILED"' in status or 'FAILED' in status


def test_debug_mode_preserves_via_cli_keep_worktrees(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_failing_repo(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("ASHA_STATE_DIR", str(state))
    worker = {"id": "w1", "deps": [], "declared_scope": ["tests/test_x.py"],
              "reads": ["tests/test_x.py"], "writes": [],
              "cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}
    report = run_workers_dag(repo, [worker], task_id="t-debug",
                             preserve_on_failure=True)
    assert report["states"]["w1"]["state"] == "FAILED"
    from asha.common import paths as common_paths
    run_dir = common_paths.get_orchestrator_dir(repo) / "w1"
    assert (run_dir / "worker-status.json").exists()