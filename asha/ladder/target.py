"""Immutable review target: exact commits, exact diff bytes, staleness check.

A review is bound to committed objects only. The context sent to a reviewer
is read from the head commit tree (``git show <sha>:<path>``), never from
the working directory, so uncommitted bytes can never be reviewed and then
approved.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from asha.ladder.models import ReviewTarget, TargetError
from asha.ladder.policy import ReviewPolicy

#: Bound for the diff text handed to a reviewer; larger diffs are truncated
#: with an explicit marker (never silently shortened).
DIFF_BUDGET = 200_000
CONTEXT_BUDGET = 60_000


class StaleTargetError(TargetError):
    """The reviewed source changed between run start and gate evaluation."""


def _git(root: Path, *args: str) -> str:
    try:
        done = subprocess.run(['git', *args], cwd=root, check=True,
                              capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise TargetError('git is not installed') from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or '').strip().splitlines()
        raise TargetError(
            f'git {" ".join(args)} failed: '
            f'{detail[-1] if detail else exc.returncode}') from exc
    return done.stdout.strip()


def resolve_commit(root: Path, ref: str) -> str:
    """Resolve any ref to a full commit SHA; invalid refs fail closed."""
    if not isinstance(ref, str) or not ref.strip():
        raise TargetError('a review ref must be a non-empty string')
    if ref.startswith('-'):
        raise TargetError(f'refusing an option-shaped ref {ref!r}')
    try:
        return _git(root, 'rev-parse', '--verify', f'{ref}^{{commit}}')
    except TargetError as exc:
        raise TargetError(f'cannot resolve ref {ref!r}: {exc}') from exc


def require_clean_tree(root: Path) -> None:
    """Refuse a dirty worktree: the target must be committed objects only."""
    status = _git(root, 'status', '--porcelain', '--untracked-files=no')
    if status:
        raise TargetError(
            'working tree has uncommitted changes to tracked files; commit '
            'or stash them first (Ladder reviews committed commits only)')


def diff_identity(root: Path, base_sha: str, head_sha: str
                  ) -> tuple[str, str, tuple[str, ...]]:
    """Return (diff text, sha256 over the exact bytes, changed files)."""
    try:
        raw = subprocess.run(
            ['git', 'diff', f'{base_sha}..{head_sha}', '--'],
            cwd=root, check=True, capture_output=True).stdout
        names = _git(root, 'diff', '--name-only', f'{base_sha}..{head_sha}')
    except subprocess.CalledProcessError as exc:
        raise TargetError(f'cannot diff {base_sha}..{head_sha}') from exc
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode('utf-8', errors='replace')
    if len(text) > DIFF_BUDGET:
        text = text[:DIFF_BUDGET] + (
            f'\n... [diff truncated at {DIFF_BUDGET} chars; '
            f'sha256 {digest} covers the full diff]')
    files = tuple(name for name in names.splitlines() if name.strip())
    return text, digest, files


def resolve_target(root: Path, *, base: str, head: str,
                   policy: ReviewPolicy, run_id: str,
                   started_at: str) -> ReviewTarget:
    """Establish the immutable target for one run."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise TargetError(f'not a directory: {root}')
    require_clean_tree(root)
    base_sha = resolve_commit(root, base)
    head_sha = resolve_commit(root, head)
    if base_sha == head_sha:
        raise TargetError(
            f'nothing to review: base and head both resolve to {base_sha[:12]}')
    try:
        branch = _git(root, 'rev-parse', '--abbrev-ref', 'HEAD')
    except TargetError:
        branch = 'HEAD'
    _, digest, files = diff_identity(root, base_sha, head_sha)
    return ReviewTarget(
        repo_root=str(root),
        branch=branch,
        base_ref=base,
        head_ref=head,
        base_sha=base_sha,
        head_sha=head_sha,
        diff_sha256=digest,
        files=files,
        policy_id=policy.policy_id,
        policy_hash=policy.content_hash(),
        run_id=run_id,
        started_at=started_at,
    )


def assert_target_unchanged(root: Path, target: ReviewTarget) -> None:
    """Re-resolve the same identities; any drift is STALE, not approval.

    Both the recorded SHAs and the refs the caller asked for are checked:
    a branch that moved mid-run means the review no longer covers what the
    caller is about to ship, even though the recorded commit is intact.
    """
    for ref, recorded in ((target.base_ref, target.base_sha),
                          (target.head_ref, target.head_sha)):
        if not ref:
            continue
        moved = resolve_commit(root, ref)
        if moved != recorded:
            raise StaleTargetError(
                f'ref {ref!r} moved during the review: {recorded[:12]} -> '
                f'{moved[:12]}; the reviewed source is no longer what this '
                'ref points at, so a new review is required')
    base_sha = resolve_commit(root, target.base_sha)
    head_sha = resolve_commit(root, target.head_sha)
    _, digest, _ = diff_identity(root, base_sha, head_sha)
    drift = {
        name: (old, new)
        for name, old, new in (
            ('base_sha', target.base_sha, base_sha),
            ('head_sha', target.head_sha, head_sha),
            ('diff_sha256', target.diff_sha256, digest),
        ) if old != new
    }
    if drift:
        raise StaleTargetError(
            'reviewed source changed during the run: '
            + '; '.join(f'{name} {old[:12]} -> {new[:12]}'
                        for name, (old, new) in drift.items()))


def file_line_count(root: Path, head_sha: str, path: str) -> int | None:
    """Line count of ``path`` in the head commit (None when absent)."""
    try:
        text = _git(root, 'show', f'{head_sha}:{path}')
    except TargetError:
        return None
    return len(text.splitlines())


def source_snippets(root: Path, head_sha: str, files: tuple[str, ...],
                    *, budget: int = CONTEXT_BUDGET) -> str:
    """Read reviewed files from the head commit (never the worktree)."""
    parts: list[str] = []
    used = 0
    for name in files:
        try:
            text = _git(root, 'show', f'{head_sha}:{name}')
        except TargetError:
            # Deleted in the head commit: the diff already carries it.
            continue
        if '\x00' in text:
            parts.append(f'### FILE: {name}\n[binary file omitted]')
            continue
        room = max(0, budget - used)
        clipped = text if len(text) <= room else (
            text[:room] + f'\n... [truncated at {room} chars]')
        parts.append(f'### FILE: {name}\n{clipped}')
        used += len(clipped)
        if used >= budget:
            parts.append(f'[context budget {budget} chars exhausted]')
            break
    return '\n\n'.join(parts) or '(no textual changes in this range)'
