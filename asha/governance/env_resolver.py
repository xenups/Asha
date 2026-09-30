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

    def resolve_for_file(self, repo_root: Path, target_file: Path) -> ServiceEnvironment:
        """Resolve the service environment for ``target_file``.

        Raises ``EnvironmentResolutionError`` when the system-Python
        fallback is explicitly disabled and no service-local Python
        exists (fail closed).
        """
        repo_root = Path(repo_root).resolve()
        service_root = self.service_root_for(repo_root, target_file)
        python_bin = service_root / ".venv" / "bin" / "python"
        pytest_bin = service_root / ".venv" / "bin" / "pytest"
        if python_bin.is_file():
            env = self._build_env_vars(repo_root, service_root)
            return ServiceEnvironment(
                service_name=service_root.name,
                service_root=service_root,
                python_bin=Path(python_bin),
                pytest_bin=Path(pytest_bin) if pytest_bin.is_file() else None,
                env_vars=env,
                is_hermetic=True,
            )
        if not self._allow_fallback:
            raise EnvironmentResolutionError(
                "no service-local Python at "
                f"{python_bin} for {service_root} and "
                f"{ALLOW_SYSTEM_PYTHON_FALLBACK_ENV} disables the "
                "system-Python fallback"
            )
        # Historical default: repository-wide interpreter.
        return ServiceEnvironment(
            service_name=service_root.name,
            service_root=service_root,
            python_bin=Path(sys.executable),
            pytest_bin=None,
            env_vars={},
            is_hermetic=False,
        )

    def _build_env_vars(self, repo_root: Path, service_root: Path) -> dict[str, str]:
        """Environment for a hermetic service: VIRTUAL_ENV, .venv/bin
        prepended to PATH, service src/ prepended to PYTHONPATH.
        Never mutates the process environment; existing PATH/PYTHONPATH
        values are preserved (prepend, never replace)."""
        venv_bin = service_root / ".venv" / "bin"
        env = dict(os.environ)
        env["VIRTUAL_ENV"] = str(service_root / ".venv")
        env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
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
) -> ServiceEnvironment:
    """Module-level convenience wrapper (default policy from env)."""
    return EnvironmentResolver(allow_system_python_fallback).resolve_for_file(
        repo_root, target_file
    )