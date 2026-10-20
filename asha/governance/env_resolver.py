"""Per-service Python environment resolution for monorepo execution.

Phase K: execution becomes aware of service-local virtualenvs instead of
assuming the repository-wide ``sys.executable``. Standalone repositories
keep their existing behavior (service root == repository root, system
Python fallback unless explicitly disabled).

Standards:
  * ``ServiceEnvironment`` is frozen and carries environment variables as
    a mapping; the resolver never mutates global ``os.environ``.
  * Service discovery walks upward from the target file and picks the
    NEAREST ancestor containing ``pyproject.toml`` or ``poetry.lock``.
  * ``is_hermetic`` means a service-local ``.venv`` was actually found --
    never merely because a marker file exists.
  * ``allow_system_python_fallback=false`` fails closed: no service-local
    Python -> raises, never silently falls back to ``sys.executable``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Env-var policy name (existing env-var config system in asha/common/paths.py).
ALLOW_SYSTEM_PYTHON_FALLBACK_ENV = "ASHA_ALLOW_SYSTEM_PYTHON_FALLBACK"

#: Marker files that define a service root.
_SERVICE_MARKERS = ("pyproject.toml", "poetry.lock")

#: ``.venv`` layouts, POSIX first: (bin dir, python name, pytest name).
#: Windows virtualenvs put the interpreter in ``Scripts/python.exe``;
#: checking only ``bin/python`` silently downgraded every Windows run to
#: the launcher interpreter (foreign site-packages, wrong package).
_VENV_LAYOUTS = (
    ("bin", "python", "pytest"),
    ("Scripts", "python.exe", "pytest.exe"),
)


def _venv_bins(service_root: Path) -> tuple[Path, Path, Path | None] | None:
    """Locate ``(python, bin dir, pytest launcher)`` in either layout."""
    for bin_dir, py_name, pytest_name in _VENV_LAYOUTS:
        venv_bin = service_root / ".venv" / bin_dir
        python_bin = venv_bin / py_name
        if python_bin.is_file():
            pytest_bin = venv_bin / pytest_name
            return (
                python_bin,
                venv_bin,
                pytest_bin if pytest_bin.is_file() else None,
            )
    return None


def _import_root(source_root: Path | str) -> Path:
    """Directory to put on ``PYTHONPATH``: ``src/`` when present (src
    layout), else the root itself (flat layout)."""
    root = Path(source_root)
    src = root / "src"
    return src if src.is_dir() else root


def _binding_env_vars(source_root: Path | str) -> dict[str, str]:
    """Import binding for a run that must see ``source_root``'s own copy.

    ``PYTHONPATH`` is REPLACED, not prepended: an inherited value or an
    editable install living in another checkout resolves the package
    elsewhere and the gate would validate the wrong bytes.
    ``PYTHONNOUSERSITE`` closes the same hole through user site-packages.
    """
    return {
        "PYTHONPATH": str(_import_root(source_root)),
        "PYTHONNOUSERSITE": "1",
    }


class EnvironmentResolutionError(Exception):
    """Fail-closed: a service environment could not be resolved."""


@dataclass(frozen=True)
class ServiceEnvironment:
    """Resolved Python environment for one service.

    ``is_hermetic`` is True only when a service-local ``.venv`` was
    detected. ``pytest_bin`` is None when no local pytest launcher exists.
    """

    service_name: str
    service_root: Path
    python_bin: Path
    pytest_bin: Path | None
    env_vars: dict[str, str]
    is_hermetic: bool
    source_root: Path | None = None


def _allow_system_python_fallback(env: Mapping[str, str]) -> bool:
    """Policy: explicit ``false``/``0`` disables fallback (fail closed).

    Any other value (unset, ``true``, ``1``) keeps the historical
    default: fall back to ``sys.executable`` when no local Python.
    """
    raw = (env.get(ALLOW_SYSTEM_PYTHON_FALLBACK_ENV) or "").strip().lower()
    return raw not in ("false", "0", "no", "off")


class EnvironmentResolver:
    """Resolve a file's service-local environment within a repository."""

    def __init__(self, allow_system_python_fallback: bool | None = None) -> None:
        self._allow_fallback = (
            _allow_system_python_fallback(os.environ)
            if allow_system_python_fallback is None
            else allow_system_python_fallback
        )

    def service_root_for(self, repo_root: Path, target_file: Path) -> Path:
        """Nearest ancestor of ``target_file`` (inclusive) with a service
        marker, never above ``repo_root``. ``target_file`` may be a file
        or a directory. Returns ``repo_root`` when no marker is found
        (standalone-repository behavior)."""
        repo_root = Path(repo_root).resolve()
        target = Path(target_file)
        start = target if target.is_dir() else target.parent
        # Walk upward, staying within repo_root.
        for candidate in (start, *start.parents):
            try:
                candidate = candidate.resolve()
            except OSError:
                continue
            if not (candidate == repo_root or repo_root in candidate.parents):
                continue
            if any((candidate / marker).is_file() for marker in _SERVICE_MARKERS):
                return candidate
            if candidate == repo_root:
                break
        return repo_root

    def resolve_for_file(
        self,
        repo_root: Path,
        target_file: Path,
        *,
        deps_root: Path | None = None,
        source_root: Path | None = None,
    ) -> ServiceEnvironment:
        """Resolve the service environment for ``target_file``.

        ``deps_root`` / ``source_root`` bind one run to TWO roots: the
        executing checkout (``repo_root``: the isolated worktree) supplies
        the source, the original checkout (``deps_root``) supplies the
        gitignored ``.venv``. Bound runs get ``PYTHONPATH`` pointed at the
        *source* root and ``PYTHONNOUSERSITE`` set, so neither an editable
        install in the original checkout nor an inherited ``PYTHONPATH``
        can shadow the worktree's own package.

        Raises ``EnvironmentResolutionError`` when the system-Python
        fallback is explicitly disabled and no service-local Python
        exists (fail closed).
        """
        repo_root = Path(repo_root).resolve()
        if deps_root is not None:
            env_root = Path(deps_root).resolve()
            try:
                # Service discovery must run in the dependency checkout:
                # the worktree has no .venv, so resolving there would find
                # nothing and fall back to the launcher interpreter.
                mapped = env_root / Path(target_file).resolve().relative_to(
                    repo_root)
            except (ValueError, OSError):
                mapped = env_root
            service_root = self.service_root_for(env_root, mapped)
        else:
            service_root = self.service_root_for(repo_root, target_file)
        found = _venv_bins(service_root)
        if found is not None:
            python_bin, venv_bin, pytest_bin = found
            env = self._build_env_vars(
                repo_root, service_root, venv_bin=venv_bin,
                source_root=source_root)
            return ServiceEnvironment(
                service_name=service_root.name,
                service_root=service_root,
                python_bin=Path(python_bin),
                pytest_bin=pytest_bin,
                env_vars=env,
                is_hermetic=True,
                source_root=(Path(source_root).resolve()
                             if source_root is not None else None),
            )
        if not self._allow_fallback:
            raise EnvironmentResolutionError(
                "no service-local Python at "
                f"{service_root / '.venv' / 'bin' / 'python'} or "
                f"{service_root / '.venv' / 'Scripts' / 'python.exe'} for "
                f"{service_root} and {ALLOW_SYSTEM_PYTHON_FALLBACK_ENV} "
                "disables the system-Python fallback"
            )
        # Historical default: repository-wide interpreter. A bound run
        # keeps that interpreter but still binds imports to the source
        # root -- losing the venue must never mean validating another
        # checkout's copy.
        return ServiceEnvironment(
            service_name=service_root.name,
            service_root=service_root,
            python_bin=Path(sys.executable),
            pytest_bin=None,
            env_vars=(_binding_env_vars(source_root)
                      if source_root is not None else {}),
            is_hermetic=False,
            source_root=(Path(source_root).resolve()
                         if source_root is not None else None),
        )

    def _build_env_vars(
        self,
        repo_root: Path,
        service_root: Path,
        *,
        venv_bin: Path | None = None,
        source_root: Path | None = None,
    ) -> dict[str, str]:
        """Environment for a hermetic service: VIRTUAL_ENV, the venv's bin
        directory prepended to PATH, and ``PYTHONPATH``.

        Unbound (historical): the service ``src/`` is prepended to the
        inherited ``PYTHONPATH`` -- prepend, never replace. Bound: the
        source root's import directory REPLACES the inherited value.
        Never mutates the process environment.
        """
        venv_bin = (venv_bin if venv_bin is not None
                    else service_root / ".venv" / "bin")
        env = dict(os.environ)
        env["VIRTUAL_ENV"] = str(service_root / ".venv")
        env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
        if source_root is not None:
            env.update(_binding_env_vars(source_root))
            return env
        src = service_root / "src"
        if src.is_dir():
            env["PYTHONPATH"] = (
                str(src) + os.pathsep + env.get("PYTHONPATH", "")
            )
        return env


def resolve_env_for_file(
    repo_root: Path,
    target_file: Path,
    *,
    allow_system_python_fallback: bool | None = None,
    deps_root: Path | None = None,
    source_root: Path | None = None,
) -> ServiceEnvironment:
    """Module-level convenience wrapper (default policy from env)."""
    return EnvironmentResolver(allow_system_python_fallback).resolve_for_file(
        repo_root,
        target_file,
        deps_root=deps_root,
        source_root=source_root,
    )