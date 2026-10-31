"""Per-invocation environment for verification subprocesses.

The tools the checks run -- python, pytest, mypy, ruff -- write caches next to
the code they analyse. With ``cwd`` set to the repository under review that is
the user's worktree, and untracked ``__pycache__/`` then made the next review
or finalize refuse with "working tree is dirty" even though nothing had been
edited.

Each verification invocation therefore gets its own environment: a copy of the
current one plus cache redirections for *its* repository. Nothing mutates
``os.environ``, so two invocations on two repositories cannot see each other's
paths, and a failure to prepare the cache directory is raised instead of
silently falling back to writing inside the target.

Measured with the versions this repository runs (python 3.12.1, ruff 0.16.9,
mypy 2.3.1, pytest 7.4.4):

===========================  ==============================================
producer                     redirection
===========================  ==============================================
CPython bytecode             ``PYTHONDONTWRITEBYTECODE=1``
pytest                       ``PYTEST_ADDOPTS`` + ``-p no:cacheprovider``
mypy                         ``MYPY_CACHE_DIR=<cache>/mypy``
ruff                         ``RUFF_CACHE_DIR=<cache>/ruff``
===========================  ==============================================
"""

from __future__ import annotations

import os
from pathlib import Path

from asha.common import paths as common_paths

CACHE_SUBDIR = "check-tools"
PYTEST_DISABLE_CACHE = "-p no:cacheprovider"


class VerificationEnvError(RuntimeError):
    """The verification cache location could not be prepared."""


def cache_root(root: Path) -> Path:
    """External cache for one repository: <state>/<repo-id>/cache/check-tools."""
    return common_paths.get_cache_dir(root) / CACHE_SUBDIR


def env_overrides(root: Path) -> dict[str, str]:
    """Cache redirections for one verification invocation on ``root``.

    The tool directories are created here so that an unwritable state root
    fails the invocation loudly; falling back to the default (in-worktree)
    cache would defeat the point.
    """
    base = cache_root(root)
    try:
        for tool in ("mypy", "ruff"):
            (base / tool).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VerificationEnvError(
            f"cannot prepare the verification cache at {base}: {exc}") from exc
    return {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": _pytest_addopts(),
        "MYPY_CACHE_DIR": str(base / "mypy"),
        "RUFF_CACHE_DIR": str(base / "ruff"),
    }


def _pytest_addopts() -> str:
    """Keep the caller's pytest options and disable the cache plugin once."""
    current = os.environ.get("PYTEST_ADDOPTS", "").strip()
    if PYTEST_DISABLE_CACHE in current:
        return current
    return (current + " " + PYTEST_DISABLE_CACHE).strip()
