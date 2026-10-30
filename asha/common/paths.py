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


def _shared_root(repo_root: Path) -> Path:
    """The MAIN worktree root for a linked worktree, else the path itself.

    Evidence is keyed by repository identity (remote URL), and a stacked-PR
    workflow checks each branch out in its own worktree. Without this, a seal
    written in a worktree is invisible to ``--stack`` run from the main
    checkout, so a fully verified stack still reads "0/N sealed".
    """
    root = Path(repo_root)
    marker = root / '.git'
    try:
        if marker.is_file():
            text = marker.read_text(encoding='utf-8', errors='replace').strip()
            if text.startswith('gitdir:'):
                gitdir = Path(text.split(':', 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = (root / gitdir).resolve()
                # <main>/.git/worktrees/<id> -> <main>; anything shallower is
                # not a linked worktree (fail back to the caller's path).
                if len(gitdir.parents) >= 3 and gitdir.parent.name == 'worktrees':
                    return gitdir.parents[2]
    except OSError:
        return root
    return root


def get_state_dir(repo_root: Path) -> Path:
    """Zone-2 root for ONE checkout: <state-root>/<checkout-id>/.

    Keyed by the resolved checkout PATH, not repository identity: two
    checkouts of one remote that happen to share a basename (clones, or
    worktrees added under same-named directories) would otherwise share
    telemetry, locks, journals and orchestrator state.
    """
    root = Path(repo_root).resolve()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return _state_root() / f"{digest}-{root.name}"


def get_verify_tip_dir(repo_root: Path) -> Path:
    """Sealed verify-tip evidence: <state-root>/<main-repo-id>/verify-tip/.

    The ONE store shared across linked worktrees: ``--stack`` reads every
    branch's seal from the main checkout.
    """
    return _state_root() / _repo_id(_shared_root(repo_root)) / "verify-tip"


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