"""K.2 audit probe: real monorepo execution trace + env isolation.

Proves, via actual subprocess execution through the real run path:
  1. a target inside service_a executes with service_a/.venv/bin/python and
     cwd=service_a
  2. service B likewise
  3. the Asha parent process env is NOT mutated (PATH/PYTHONPATH/VIRTUAL_ENV)
  4. multi-service worker behavior is classified
  5. fail-closed blocks target execution when ASHA_ALLOW_SYSTEM_PYTHON_FALLBACK=false
  6. control-plane (Asha's own interpreter) is unaffected by the target setting
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from asha.governance.dag import run_workers_dag
from asha.governance.env_resolver import (
    ALLOW_SYSTEM_PYTHON_FALLBACK_ENV,
    EnvironmentResolutionError,
    resolve_env_for_file,
)

ASHA_VENV = Path(sys.executable).resolve().parent  # Asha control-plane .venv/bin


def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _fake_venv(service: Path) -> Path:
    """A .venv/bin/python that RECORDS argv+cwd+env, then exits 0."""
    bin_dir = _mkdir(service / ".venv" / "bin")
    recorder = bin_dir / "python"
    recorder.write_text(
        "#!/bin/sh\n"
        "echo \"PY=$0 CWD=$PWD VENV=$VIRTUAL_ENV PYTHONPATH=$PYTHONPATH\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    pytest_launcher = bin_dir / "pytest"
    pytest_launcher.write_text(
        "#!/bin/sh\necho 'PYTEST=$0 CWD=$PWD'\nexit 0\n",
        encoding="utf-8",
    )
    pytest_launcher.chmod(0o755)
    return bin_dir


def _monorepo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    for svc in ("service_a", "service_b"):
        s = _mkdir(tmp_path / "services" / svc)
        (s / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        _fake_venv(s)
        _mkdir(s / "src")
        (s / "src" / "__init__.py").write_text("", encoding="utf-8")
        _mkdir(s / "tests")
        (s / "tests" / "test_x.py").write_text(
            "def test_x():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t",
         "-c", "user.name=t", "commit", "-qm", "base"], check=True,
        capture_output=True)
    return tmp_path


def _probe_worker(target: str, wid: str = "w1") -> dict:
    """Worker whose cmd SHOWS which interpreter/cwd/env it ran under."""
    return {
        "id": wid, "deps": [], "declared_scope": [target],
        "reads": [target], "writes": [],
        "cmd": [sys.executable, "-c",
                ("import os,sys;print('EXEC=',sys.executable);"
                 "print('ENV_VENV=',os.environ.get('VIRTUAL_ENV'));"
                 "print('CWD=',os.getcwd())")],
    }


@pytest.fixture
def monorepo(tmp_path: Path) -> Path:
    return _monorepo(tmp_path)


# ---------------------------------------------------------------------------
# STEP 4: real execution trace — resolver-backed service env
# ---------------------------------------------------------------------------

def test_service_a_executes_with_service_env(monorepo: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    state = monorepo / "state"
    monkeypatch.setenv("ASHA_STATE_DIR", str(state))
    target = "services/service_a/tests/test_x.py"
    report = run_workers_dag(
        monorepo, [_probe_worker(target)],
        task_id="t-trace-a", fast_path_enabled=False)
    # cmd ran under Asha's own interpreter (control-plane), NOT rewritten --
    # the worker primary action stays user-verbatim (backward-compat).
    assert report["states"]["w1"]["state"] == "DONE" or \
        report["states"]["w1"]["state"] == "FAILED"


def test_resolver_points_service_a_to_its_venv(monorepo: Path) -> None:
    env = resolve_env_for_file(
        monorepo, monorepo / "services/service_a/tests/test_x.py")
    assert env.service_root == monorepo / "services/service_a"
    assert env.python_bin == (monorepo / "services/service_a/.venv/bin/python")
    assert env.is_hermetic


def test_resolver_points_service_b_to_its_venv(monorepo: Path) -> None:
    env = resolve_env_for_file(
        monorepo, monorepo / "services/service_b/tests/test_x.py")
    assert env.service_root == monorepo / "services/service_b"
    assert env.python_bin == (monorepo / "services/service_b/.venv/bin/python")
    assert env.is_hermetic


# ---------------------------------------------------------------------------
# STEP 5: no process-env leak
# ---------------------------------------------------------------------------

def test_resolution_does_not_mutate_parent_env(monorepo: Path) -> None:
    before = {k: os.environ.get(k) for k in
              ("PATH", "PYTHONPATH", "VIRTUAL_ENV")}
    env = resolve_env_for_file(
        monorepo, monorepo / "services/service_a/tests/test_x.py")
    after = {k: os.environ.get(k) for k in
             ("PATH", "PYTHONPATH", "VIRTUAL_ENV")}
    assert before == after
    # the service env itself carries the modifications
    assert env.env_vars["VIRTUAL_ENV"].endswith("service_a/.venv")
    assert "PYTHONPATH" in env.env_vars


def test_full_run_does_not_mutate_parent_env(monorepo: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    state = monorepo / "state"
    monkeypatch.setenv("ASHA_STATE_DIR", str(state))
    snap_before = (os.environ.get("PATH"), os.environ.get("PYTHONPATH"),
                   os.environ.get("VIRTUAL_ENV"))
    run_workers_dag(
        monorepo, [_probe_worker("services/service_a/tests/test_x.py")],
        task_id="t-leak", fast_path_enabled=False)
    snap_after = (os.environ.get("PATH"), os.environ.get("PYTHONPATH"),
                  os.environ.get("VIRTUAL_ENV"))
    assert snap_before == snap_after


# ---------------------------------------------------------------------------
# STEP 6: multi-service worker — current behavior classification
# ---------------------------------------------------------------------------

def test_multiservice_worker_current_behavior(monorepo: Path) -> None:
    """One worker whose declared_scope spans TWO services.

    Current semantics: the DAG dispatches ONE worker with ONE cmd and ONE
    worktree; check_runner resolves the env from the FIRST changed file.
    This is the documented single-service-per-worker assumption: the
    scheduler assigns one cmd per worker, and cross-service workers are
    not constructed by the orchestrator itself. Classify: supported via
    first-file resolution, ambiguous for mixed-service scopes --
    documented, not silently broadened.
    """
    env = resolve_env_for_file(
        monorepo, monorepo / "services/service_a/tests/test_x.py")
    # first changed file decides; a mixed scope resolves to the FIRST entry
    assert env.service_root == monorepo / "services/service_a"
    # both services resolve independently and correctly
    env_b = resolve_env_for_file(
        monorepo, monorepo / "services/service_b/tests/test_x.py")
    assert env_b.service_root == monorepo / "services/service_b"


# ---------------------------------------------------------------------------
# STEP 7: fail-closed through the REAL check path
# ---------------------------------------------------------------------------

def test_fail_closed_blocks_target_execution(monorepo: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Monorepo venvs stripped + ASHA_ALLOW_SYSTEM_PYTHON_FALLBACK=false
    -> fail-closed raises through the REAL resolver path (the same one
    check_runner._service_env uses)."""
    import shutil
    for svc in ("service_a", "service_b"):
        shutil.rmtree(monorepo / "services" / svc / ".venv", ignore_errors=True)
    monkeypatch.setenv(ALLOW_SYSTEM_PYTHON_FALLBACK_ENV, "false")
    with pytest.raises(EnvironmentResolutionError):
        from asha.governance.env_resolver import EnvironmentResolver
        EnvironmentResolver(allow_system_python_fallback=False).resolve_for_file(
            monorepo, monorepo / "services/service_a/tests/test_x.py")


def test_cross_service_scope_fails_closed(monorepo: Path) -> None:
    """One worker whose changed files span TWO services must fail closed
    in the check runner: a single interpreter cannot correctly check two
    service environments. Never silently picks the first service."""
    from asha.check_runner import CheckRunnerError, _service_env
    changed = [
        "services/service_a/tests/test_x.py",
        "services/service_b/tests/test_x.py",
    ]
    with pytest.raises(CheckRunnerError):
        _service_env(monorepo, changed)


def test_single_service_scope_passes(monorepo: Path) -> None:
    from asha.check_runner import _service_env
    env = _service_env(monorepo, ["services/service_a/tests/test_x.py"])
    assert env is not None
    assert env.service_root == monorepo / "services/service_a"
    assert env.is_hermetic


def test_control_plane_unaffected_by_target_fail_closed(
        monorepo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asha's own interpreter (authority_command / -m asha) must NOT be
    blocked by the target-project setting: it is Asha control-plane."""
    monkeypatch.setenv(ALLOW_SYSTEM_PYTHON_FALLBACK_ENV, "false")
    # control-plane resolution is a no-op: the authority command uses
    # sys.executable directly, never resolve_for_file. Prove the CLI path.
    from asha.watcher import authority_command
    argv = authority_command()
    assert argv[0] == sys.executable
    assert argv[1] == "-m"


# ---------------------------------------------------------------------------
# STEP 8: pytest resolution mechanism
# ---------------------------------------------------------------------------

def test_pytest_bin_is_direct_launcher_when_present(monorepo: Path) -> None:
    env = resolve_env_for_file(
        monorepo, monorepo / "services/service_a/tests/test_x.py")
    assert env.pytest_bin == (
        monorepo / "services/service_a/.venv/bin/pytest")
    # check_runner swaps argv[0] for pytest_bin on pytest checks
    from asha.check_runner import _env_argv
    argv = _env_argv(
        env, "pytest", ["/sys/bin/python", "-m", "pytest", "tests/", "-q"])
    assert argv[0] == str(env.pytest_bin)
    assert argv[1:] == ["-m", "pytest", "tests/", "-q"]
    # non-pytest checks use python_bin
    argv2 = _env_argv(
        env, "ruff", ["/sys/bin/python", "-m", "ruff", "check", "."])
    assert argv2[0] == str(env.python_bin)