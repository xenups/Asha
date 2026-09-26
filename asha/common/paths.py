"""Externalized filesystem-state path resolution (Phase D).

Single responsibility: logical state kind + repository/run identity
  -> physical external path.

Zones:
  Zone 2 (persistent) : $ASHA_STATE_DIR | $XDG_STATE_HOME/asha | ~/.local/state/asha
                          <root>/evidence/<repo-id>/
                          <root>/journal/<repo-id>/
                          <root>/orchestrator/<repo-id>/
                          <root>/cache/<repo-id>/
  Zone 3 (ephemeral)  : $TMPDIR/asha-<run-id>/   (platform temp)

Pure resolver: NO side effects (no mkdir, no writes). Path creation
belongs to the writer/lifecycle layer.

Repo identity reuses the EXISTING scheme (memory.repo_identity: remote
URL else absolute path) hashed for collision-resistance — never the raw
path as a filename, never the bare basename.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from asha.memory import repo_identity


def _state_root() -> Path:
    """Persistent Asha state root (Zone 2), no side effects."""
    explicit = os.environ.get("ASHA_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "asha"
    return Path.home() / ".local" / "state" / "asha"


def _repo_id(repo_root: Path) -> str:
    """Deterministic, collision-resistant repository identity.

    Derived from the EXISTING identity scheme (remote URL else absolute
    path) via SHA-256; hex keeps filenames safe across platforms.
    """
    identity = repo_identity(repo_root)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{digest}-{Path(repo_root).name}"


def get_state_dir(repo_root: Path) -> Path:
    """Zone-2 root for a repository: <state-root>/<repo-id>/."""
    return _state_root() / _repo_id(Path(repo_root))


def get_evidence_dir(repo_root: Path) -> Path:
    """Evidence persistence: <state-root>/<repo-id>/evidence/."""
    return get_state_dir(repo_root) / "evidence"


def get_journal_dir(repo_root: Path) -> Path:
    """Journal persistence: <state-root>/<repo-id>/journal/."""
    return get_state_dir(repo_root) / "journal"


def get_orchestrator_dir(repo_root: Path) -> Path:
    """Orchestrator worker-evidence + cache: <state>/<repo-id>/orchestrator/."""
    return get_state_dir(repo_root) / "orchestrator"


def get_cache_dir(repo_root: Path) -> Path:
    """Caches (orient, mem0): <state-root>/<repo-id>/cache/."""
    return get_state_dir(repo_root) / "cache"


def get_scratch_dir(run_id: str) -> Path:
    """Zone-3 ephemeral scratch: <tmpdir>/asha-<run-id>/."""
    return Path(tempfile.gettempdir()) / f"asha-{run_id}"


def resolve_checked(kind: str, repo_root: Path, run_id: str | None = None) -> Path:
    """Resolve a logical kind to an external path, fail-closed.

    kinds: evidence | journal | orchestrator | cache | scratch
    Raises if the resolved path would land under the repository.
    """
    root = Path(repo_root).resolve()
    if kind == "scratch":
        if not run_id:
            raise ValueError("scratch kind requires run_id")
        path = get_scratch_dir(run_id)
    elif kind == "evidence":
        path = get_evidence_dir(root)
    elif kind == "journal":
        path = get_journal_dir(root)
    elif kind == "orchestrator":
        path = get_orchestrator_dir(root)
    elif kind == "cache":
        path = get_cache_dir(root)
    else:
        raise ValueError(f"unknown state kind: {kind}")
    resolved = path.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError(
            f"state path {resolved} would land inside the repo {root}"
        )
    return path