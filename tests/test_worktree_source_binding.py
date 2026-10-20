"""Worktree-local execution: the gate must validate the WORKTREE's source.

A git worktree carries only TRACKED files, so a gitignored ``.venv`` never
exists there. Two failures followed from that:

* ``env_resolver`` only looked for the POSIX ``.venv/bin/python``, so a
  Windows ``.venv/Scripts/python.exe`` was never detected;
* with no venv, the check run fell back to the launcher interpreter and
  inherited the process ``PYTHONPATH``, so an editable install (or any
  other checkout on the path) supplied the package while the gate reported
  a verdict about the worktree.

These tests pin the invariant: a bound run resolves dependencies from the
original checkout but IMPORTS from the worktree, and loses the venue only
as an explicit failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from asha import check_runner
from asha.governance.env_resolver import (
    EnvironmentResolutionError,
    EnvironmentResolver,
    resolve_env_for_file,
)

RESOLVED: dict[str, Any] = {
    'checks': ['pytest'],
    'affected_files': ['src/pkg/__init__.py'],
    'per_file': {},
    'scope': 'S2',
    'base': None,
}


def _repo(tmp_path: Path, name: str, *, value: str = 'v0',
          layout: str = 'src', with_tests_dir: bool = True) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / 'pyproject.toml').write_text('[project]\nname = "fixture"\n')
    pkg = (root / 'src' / 'pkg') if layout == 'src' else (root / 'pkg')
    pkg.mkdir(parents=True)
    (pkg / '__init__.py').write_text(f'VALUE = {value!r}\n')
    if with_tests_dir:
        tests = root / 'tests'
        tests.mkdir()
        body = 'import pkg\n\n\ndef test_value():\n    assert pkg.VALUE == "v0"\n'
        (tests / 'test_pkg.py').write_text(body)
    else:
        (root / 'test_pkg.py').write_text(
            'import pkg\n\n\ndef test_value():\n    assert pkg.VALUE == "v0"\n')
    return root


def _fake_venv(root: Path, *parts: str) -> Path:
    """Create a venv-shaped python file; resolution is path-only."""
    python_bin = root.joinpath('.venv', *parts)
    python_bin.parent.mkdir(parents=True, exist_ok=True)
    python_bin.write_text('')
    return python_bin


# --- 1. interpreter discovery on both venv layouts -------------------------


def test_posix_venv_layout_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv('PYTHONPATH', raising=False)
    _repo(tmp_path, '.')
    python_bin = _fake_venv(tmp_path, 'bin', 'python')
    env = resolve_env_for_file(tmp_path, tmp_path / 'src' / 'pkg' / '__init__.py')
    assert env.is_hermetic is True
    assert env.python_bin == python_bin
    assert env.env_vars['PYTHONPATH'].startswith(str(tmp_path / 'src'))


def test_windows_venv_layout_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv('PYTHONPATH', raising=False)
    python_bin = _fake_venv(tmp_path, 'Scripts', 'python.exe')
    env = resolve_env_for_file(tmp_path, tmp_path / 'src' / 'pkg' / '__init__.py')
    assert env.is_hermetic is True, 'Scripts/python.exe must count as hermetic'
    assert env.python_bin == python_bin
    assert str(python_bin.parent) in env.env_vars['PATH']


def test_windows_venv_pytest_launcher(tmp_path, monkeypatch):
    monkeypatch.delenv('PYTHONPATH', raising=False)
    _fake_venv(tmp_path, 'Scripts', 'python.exe')
    pytest_bin = _fake_venv(tmp_path, 'Scripts', 'pytest.exe')
    env = resolve_env_for_file(tmp_path, tmp_path / 'src' / 'pkg' / '__init__.py')
    assert env.pytest_bin == pytest_bin


# --- 2. bound worktree run: deps from checkout, source from worktree ------


def test_bound_worktree_resolves_deps_from_original_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv('PYTHONPATH', raising=False)
    original = _repo(tmp_path, 'original')
    worktree = _repo(tmp_path, 'worktree', value='worktree')
    _fake_venv(original, 'Scripts', 'python.exe')

    env = resolve_env_for_file(
        worktree, worktree / 'src' / 'pkg' / '__init__.py',
        deps_root=original, source_root=worktree)

    assert env.is_hermetic is True
    assert env.python_bin == original / '.venv' / 'Scripts' / 'python.exe'
    assert env.source_root == worktree.resolve()
    assert env.env_vars['PYTHONPATH'] == str((worktree / 'src').resolve())
    assert env.env_vars['PYTHONNOUSERSITE'] == '1'


def test_bound_env_replaces_inherited_pythonpath(tmp_path, monkeypatch):
    foreign = _repo(tmp_path, 'foreign', value='foreign')
    worktree = _repo(tmp_path, 'worktree', value='worktree')
    monkeypatch.setenv('PYTHONPATH', str(foreign / 'src'))

    env = resolve_env_for_file(
        worktree, worktree / 'src' / 'pkg' / '__init__.py',
        deps_root=foreign, source_root=worktree)

    assert env.env_vars['PYTHONPATH'] == str((worktree / 'src').resolve())
    assert str(foreign / 'src') not in env.env_vars['PYTHONPATH']


# --- 3. import identity: the worktree's copy is what gets imported --------


def test_source_binding_probe_returns_worktree_package(tmp_path, monkeypatch):
    original = _repo(tmp_path, 'original', value='original')
    worktree = _repo(tmp_path, 'worktree', value='worktree')
    monkeypatch.setenv('PYTHONPATH', str(original / 'src'))

    env = resolve_env_for_file(
        worktree, worktree / 'src' / 'pkg' / '__init__.py',
        deps_root=original, source_root=worktree)
    origins = check_runner.verify_source_binding(
        env, worktree, ['src/pkg/__init__.py'])

    assert 'pkg' in origins
    assert Path(origins['pkg']).resolve() == (worktree / 'src' / 'pkg').resolve()
    assert Path(origins['pkg']).resolve() != (original / 'src' / 'pkg').resolve()


def test_gate_uses_worktree_copy_not_original_checkout(tmp_path, monkeypatch):
    """Same test file, two different package bodies: the verdict follows
    the WORKTREE. Unbound, the very same run cannot import at all."""
    monkeypatch.delenv('PYTHONPATH', raising=False)
    original = _repo(tmp_path, 'original', value='v0')
    worktree = _repo(tmp_path, 'worktree', value='v0')

    bound = check_runner.run(worktree, dict(RESOLVED), deps_root=original)
    assert [c['status'] for c in bound] == ['passed'], bound

    # Break ONLY the worktree: the original checkout stays green.
    (worktree / 'src' / 'pkg' / '__init__.py').write_text('VALUE = "broken"\n')
    broken = check_runner.run(worktree, dict(RESOLVED), deps_root=original)
    assert broken[0]['status'] == 'failed'
    assert 'ValueError' in broken[0]['output_tail'] or 'assert' in broken[0]['output_tail']


def test_unbound_run_does_not_read_another_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv('PYTHONPATH', raising=False)
    _repo(tmp_path, 'original', value='v0')  # sibling checkout exists
    worktree = _repo(tmp_path, 'worktree', value='v0')

    unbound = check_runner.run(worktree, dict(RESOLVED))
    assert unbound[0]['status'] == 'failed'
    assert unbound[0]['exit_code'] != 0


# --- 4. fail closed when the venue cannot be established -----------------


def _link_dir(target: Path, link: Path) -> None:
    """Directory junction/symlink (no admin needed on Windows)."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if sys.platform != 'win32':
            pytest.skip('directory links unsupported on this platform')
    proc = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(link), str(target)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f'cannot create directory link: {proc.stderr.strip()}')


def test_source_binding_rejects_a_checkout_reached_through_a_link(tmp_path, monkeypatch):
    """An import whose real location is another checkout must not PASS."""
    monkeypatch.delenv('PYTHONPATH', raising=False)
    original = _repo(tmp_path, 'original', value='v0')
    worktree = _repo(tmp_path, 'worktree', value='v0')
    shutil.rmtree(worktree / 'src' / 'pkg')
    _link_dir(original / 'src' / 'pkg', worktree / 'src' / 'pkg')

    with pytest.raises(check_runner.CheckRunnerError):
        check_runner.run(worktree, dict(RESOLVED), deps_root=original)


def test_resolution_fails_closed_without_local_python(tmp_path):
    with pytest.raises(EnvironmentResolutionError):
        EnvironmentResolver(allow_system_python_fallback=False).resolve_for_file(
            tmp_path, tmp_path / 'src' / 'pkg' / '__init__.py')


def test_broken_interpreter_never_passes(tmp_path, monkeypatch):
    """A venv python that cannot run is an execution error, never a PASS."""
    monkeypatch.delenv('PYTHONPATH', raising=False)
    original = _repo(tmp_path, 'original', value='v0')
    worktree = _repo(tmp_path, 'worktree', value='v0')
    _fake_venv(original, 'bin', 'python')  # empty file: not an interpreter

    with pytest.raises(check_runner.CheckRunnerError):
        check_runner.run(worktree, dict(RESOLVED), deps_root=original)


# --- 5. unbound behavior for supported layouts stays intact -------------


def test_flat_layout_with_tests_dir_still_passes(tmp_path, monkeypatch):
    monkeypatch.delenv('PYTHONPATH', raising=False)
    repo = _repo(tmp_path, 'flat', layout='flat')
    checks = check_runner.run(repo, dict(RESOLVED))
    assert [c['status'] for c in checks] == ['passed']


def test_root_level_suite_without_tests_dir_still_passes(tmp_path, monkeypatch):
    monkeypatch.delenv('PYTHONPATH', raising=False)
    repo = _repo(tmp_path, 'rootsuite', layout='flat', with_tests_dir=False)
    checks = check_runner.run(repo, dict(RESOLVED))
    assert [c['status'] for c in checks] == ['passed']


def test_service_root_from_original_checkout_is_not_the_execution_cwd(tmp_path, monkeypatch):
    """Bound runs execute at the worktree, never at the dependency checkout."""
    monkeypatch.delenv('PYTHONPATH', raising=False)
    original = _repo(tmp_path, 'original')
    worktree = _repo(tmp_path, 'worktree', value='worktree')
    _fake_venv(original, 'Scripts', 'python.exe')

    env = resolve_env_for_file(
        worktree, worktree / 'src' / 'pkg' / '__init__.py',
        deps_root=original, source_root=worktree)
    cwd, proc_env = check_runner._env_cwd_env(env, worktree)
    assert cwd == worktree
    assert Path(proc_env['PYTHONPATH']) == (worktree / 'src').resolve()
    assert os.pathsep in env.env_vars['PATH']
    assert sys.executable  # interpreter provenance stays explicit
