"""Regression: execution-time test-path conversion.

The governance representation of targeted_tests is repository-relative
(scoping derives it from rel_of_module). Execution, however, runs with
cwd = service_root when the environment is hermetic. Conversion must
happen ONLY at the execution boundary, must fail closed for a test
outside the selected service, and must be a no-op for non-hermetic /
single-root environments (historical behavior).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from asha.check_runner import CheckRunnerError, _exec_path_args
from asha.governance.env_resolver import (
    EnvironmentResolver,
    ServiceEnvironment,
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo with one service component (services/service_a)."""
    root = tmp_path / "repo"
    svc = root / "services" / "service_a"
    (svc / ".venv" / "bin").mkdir(parents=True)
    (svc / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (svc / ".venv" / "bin" / "pytest").write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
    (svc / "tests").mkdir(parents=True)
    (svc / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (svc / "tests" / "test_thing.py").write_text("def test_x(): pass\n", encoding="utf-8")
    return root


def _env(root: Path) -> ServiceEnvironment:
    env = EnvironmentResolver().resolve_for_file(
        root, root / "services" / "service_a" / "tests" / "test_thing.py")
    assert env.is_hermetic
    return env


def test_repo_relative_to_service_relative(repo: Path) -> None:
    env = _env(repo)
    out = _exec_path_args(env, repo, ["services/service_a/tests/test_thing.py"])
    assert out == ["tests/test_thing.py"]


def test_multiple_paths_mixed(repo: Path) -> None:
    env = _env(repo)
    out = _exec_path_args(
        env, repo,
        ["services/service_a/tests/a.py", "services/service_a/tests/b.py"])
    assert out == ["tests/a.py", "tests/b.py"]


def test_outside_service_fails_closed(repo: Path) -> None:
    env = _env(repo)
    with pytest.raises(CheckRunnerError, match="outside selected service"):
        _exec_path_args(env, repo, ["other_component/tests/x.py"])


def test_root_env_passthrough(repo: Path) -> None:
    # Non-hermetic (no service pyproject->.venv match): unchanged.
    env = EnvironmentResolver().resolve_for_file(repo, repo)
    assert env.is_hermetic is False
    out = _exec_path_args(env, repo, ["tests/z.py"])
    assert out == ["tests/z.py"]


def test_single_root_no_conversion(repo: Path) -> None:
    # service_root == repo root => identity, even if hermetic.
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    env = EnvironmentResolver().resolve_for_file(repo, repo)
    assert env.is_hermetic
    out = _exec_path_args(env, repo, ["tests/a.py"])
    assert out == ["tests/a.py"]


def test_none_env_passthrough(repo: Path) -> None:
    out = _exec_path_args(None, repo, ["a.py"])
    assert out == ["a.py"]

def test_pytest_launcher_drops_module_flag(repo: Path) -> None:
    """pytest_bin is a launcher: `-m` must not survive as a marker filter."""
    from asha.check_runner import _env_argv
    env = _env(repo)
    assert env.pytest_bin is not None
    argv = ["py", "-m", "pytest", "tests/a.py", "-q"]
    out = _env_argv(env, "pytest", argv)
    assert out[0] == str(env.pytest_bin)
    assert "-m" not in out
    assert out == [str(env.pytest_bin), "tests/a.py", "-q"]


def test_other_checks_keep_module_flag(repo: Path) -> None:
    """ruff/mypy keep `-m <module>` (python -m invocation)."""
    from asha.check_runner import _env_argv
    env = _env(repo)
    out = _env_argv(env, "ruff", ["py", "-m", "ruff", "check", "a.py"])
    assert out[1] == "-m"
    assert out[0] == str(env.python_bin)
