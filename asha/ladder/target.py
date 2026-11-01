"""Immutable review target and bounded, read-only context serving.

A review is bound to committed objects only. Context is read from the
recorded head commit (``git show <sha>:<path>``), never from the working
directory, so uncommitted bytes can neither be reviewed nor approved.

This module is the only place that runs git for a review, and every
argument it passes is constructed here: no caller can supply an option, a
path that escapes the repository, or a command.
"""

from __future__ import annotations

import hashlib
import posixpath
import subprocess
from pathlib import Path

from asha.ladder.models import ReviewTarget, TargetError
from asha.ladder.policy import ReviewPolicy

#: Bound for the diff text folded into a target fingerprint.
DIFF_BUDGET = 200_000

#: Bounds for one context response; the host pages with a cursor.
DEFAULT_CONTEXT_BYTES = 20_000
MAX_CONTEXT_BYTES = 100_000

#: Paths a review never serves, whatever git tracks.
SECRET_NAMES = frozenset({
    '.env', '.netrc', '.pgpass', '.npmrc', '.pypirc',
    'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519',
    'credentials', 'secrets', 'secrets.json', 'credentials.json',
})
SECRET_SUFFIXES = ('.pem', '.key', '.p12', '.pfx', '.keystore', '.jks',
                   '.kdbx', '.ppk')


class StaleTargetError(TargetError):
    """The reviewed source changed between submission and gate evaluation."""


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


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(['git', *args], cwd=root, check=True,
                              capture_output=True)
    except FileNotFoundError as exc:
        raise TargetError('git is not installed') from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b'').decode('utf-8', 'replace').strip()
        raise TargetError(f'git {" ".join(args)} failed: '
                          f'{detail.splitlines()[-1] if detail else exc.returncode}') from exc
    return done.stdout


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

    A ref pinned by commit id cannot move, so the content actually checked
    out is compared as well: the gate audits the working tree, and
    authorizing a tree the review never saw is the failure this function
    exists to prevent. Trees are compared with trees -- equal content at a
    different commit stays reviewable.
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
    reviewed_tree = _git(root, 'rev-parse', f'{head_sha}^{{tree}}')
    checkout_tree = _git(root, 'rev-parse', 'HEAD^{tree}')
    if reviewed_tree != checkout_tree:
        raise StaleTargetError(
            f'checked-out tree is not the reviewed tree: {reviewed_tree[:12]} '
            f'-> {checkout_tree[:12]}; the gate audits the working tree, so '
            'the reviewed content must be what is checked out')


def is_secret_path(path: str) -> bool:
    """Refuse credentials-shaped paths before touching git."""
    base = posixpath.basename(path).lower()
    if base in SECRET_NAMES or base.startswith('.env'):
        return True
    if base.endswith(SECRET_SUFFIXES):
        return True
    return 'secret' in base or 'credential' in base


def tracked_at(root: Path, sha: str, path: str) -> bool:
    """Is ``path`` a file in the ``sha`` tree? (no traversal: literal path)"""
    if is_secret_path(path):
        return False
    try:
        subprocess.run(['git', 'cat-file', '-e', f'{sha}:{path}'], cwd=root,
                       check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return True


def file_text(root: Path, sha: str, path: str) -> str:
    """Whole file content at ``sha``; the caller bounds it."""
    return _git(root, 'show', f'{sha}:{path}')


def file_line_count(root: Path, sha: str, path: str) -> int | None:
    """Line count of ``path`` in ``sha`` (None when absent)."""
    try:
        text = file_text(root, sha, path)
    except TargetError:
        return None
    return len(text.splitlines())


def _chunk(payload: bytes, cursor: int, max_bytes: int) -> dict[str, object]:
    # Defence in depth: the orchestrator validates max_bytes, but the
    # chunker is the last place a caller could ask for an unbounded reply.
    max_bytes = max(1, min(int(max_bytes), MAX_CONTEXT_BYTES))
    cursor = max(0, min(int(cursor), len(payload)))
    end = min(len(payload), cursor + max_bytes)
    body = payload[cursor:end]
    return {
        'cursor': cursor,
        'next_cursor': None if end >= len(payload) else end,
        'complete': end >= len(payload),
        'total_bytes': len(payload),
        'chunk_sha256': hashlib.sha256(body).hexdigest(),
        'content': body.decode('utf-8', errors='replace'),
        'line_start': payload[:cursor].count(b'\n') + 1,
        'line_end': payload[:end].count(b'\n') + (0 if end and
                                                  payload[end - 1:end] == b'\n'
                                                  else 1),
    }


def diff_chunk(root: Path, target: ReviewTarget, *, cursor: int = 0,
               max_bytes: int = DEFAULT_CONTEXT_BYTES) -> dict[str, object]:
    """One paginated slice of the exact reviewed diff."""
    raw = _git_bytes(root, 'diff', f'{target.base_sha}..{target.head_sha}',
                     '--')
    chunk = _chunk(raw, cursor, max_bytes)
    chunk.update({
        'kind': 'diff',
        'review_run_id': target.run_id,
        'base_sha': target.base_sha,
        'head_sha': target.head_sha,
        'diff_sha256': target.diff_sha256,
        'changed_files': list(target.files),
    })
    return chunk


def file_chunk(root: Path, target: ReviewTarget, path: str, *,
               cursor: int = 0,
               max_bytes: int = DEFAULT_CONTEXT_BYTES) -> dict[str, object]:
    """One paginated slice of a file as it exists at the reviewed head."""
    if is_secret_path(path):
        raise TargetError(
            f'refusing to serve credential-shaped path {path!r}')
    if not tracked_at(root, target.head_sha, path):
        raise TargetError(
            f'{path!r} is not a file in the reviewed commit '
            f'{target.head_sha[:12]}')
    raw = _git_bytes(root, 'show', f'{target.head_sha}:{path}')
    chunk = _chunk(raw, cursor, max_bytes)
    chunk.update({
        'kind': 'file',
        'review_run_id': target.run_id,
        'base_sha': target.base_sha,
        'head_sha': target.head_sha,
        'path': path,
        'in_changed_files': path in target.files,
    })
    return chunk


def source_snippets(root: Path, head_sha: str, files: tuple[str, ...],
                    *, budget: int = 60_000) -> str:
    """Read reviewed files from the head commit (never the worktree)."""
    parts: list[str] = []
    used = 0
    for name in files:
        if is_secret_path(name):
            continue
        try:
            text = file_text(root, head_sha, name)
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
