"""Git fact discovery: repository root and one coherent working-state
snapshot.

This module only reports WHAT git sees: a normalized, immutable path
set plus per-path working states (staged, unstaged, deleted, untracked,
renamed) and merge-conflict presence. Git is an implementation detail;
callers receive a single snapshot derived from one coherent repository
read, never a concatenation of naive command outputs.

It computes no governance outcomes of any kind -- those belong
exclusively to the engine and its frozen core.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Working-state tags (facts, not interpretations).
TAG_STAGED = 'staged'
TAG_UNSTAGED = 'unstaged'
TAG_UNTRACKED = 'untracked'
TAG_DELETED = 'deleted'
TAG_RENAMED = 'renamed'
TAG_CONFLICTED = 'conflicted'

# porcelain XY codes that mean an unmerged (conflict) entry.
_CONFLICT_CODES = {'DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'}


class GitContextError(Exception):
    """Operational repository-state problem (not a governance outcome).

    ``code`` is a stable machine string surfaced by the CLI as an
    operational error (exit code 2).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(['git', *args], cwd=root, capture_output=True,
                          text=True, encoding='utf-8', errors='replace',
                          timeout=60)
    if check and proc.returncode != 0:
        raise GitContextError(
            'REPOSITORY_ERROR',
            'git ' + ' '.join(args) + ': ' + (proc.stderr or '').strip())
    return proc.stdout


@dataclass(frozen=True, slots=True)
class WorkingState:
    """Immutable normalized snapshot of the working repository state.

    Built once by :func:`snapshot`; every consumer receives copies.
    """

    root: Path
    base: str | None
    paths: tuple[str, ...]              # membership (scope-resolver source)
    states: Mapping[str, tuple[str, ...]]   # path -> sorted state tags
    conflicts: tuple[str, ...]          # unmerged paths (operational)
    untracked: tuple[str, ...]

    def as_change_set_detail(self) -> dict[str, object]:
        """JSON-safe additive detail for the change_set payload slot."""
        return {
            'states': {path: list(tags)
                       for path, tags in sorted(self.states.items())},
            'untracked': list(self.untracked),
            'conflicts': list(self.conflicts),
        }


def discover_root(start: Path | str) -> Path:
    """Resolve ``start`` (or the first git toplevel above it) to the
    repository root. Raises GitContextError outside a repository."""
    start = Path(start).resolve()
    probe = start if start.is_dir() else start.parent
    if not probe.is_dir():
        raise GitContextError('REPOSITORY_ERROR',
                              f'root is not a directory: {probe}')
    toplevel = _git(probe, 'rev-parse', '--show-toplevel', check=False).strip()
    if not toplevel:
        raise GitContextError('REPOSITORY_ERROR', 'not a git repository')
    return Path(toplevel).resolve()


def _parse_porcelain_v1_z(raw: str) -> tuple[dict[str, tuple[str, ...]],
                                             tuple[str, ...],
                                             tuple[str, ...]]:
    """Single atomic parse of one ``status --porcelain=v1 -z`` read.

    Returns (path -> tags, conflicts, renames). ``-z`` records are
    NUL-terminated; rename/copy entries carry the source path as the
    following record, so the overlay semantics stay coherent: a path
    with both a staged and an unstaged side gets both tags from ONE row.
    """
    records = [rec for rec in raw.split('\0') if rec]
    states: dict[str, tuple[str, ...]] = {}
    conflicts: list[str] = []
    renames: list[str] = []
    index = 0
    while index < len(records):
        entry = records[index]
        index += 1
        if len(entry) < 4 or entry[2] != ' ':
            # malformed row: ignore rather than guess
            continue
        xy, path = entry[:2], entry[3:]
        if (xy[0] in 'RC' or xy[1] in 'RC') and index < len(records):
            # rename/copy: source path is the next NUL record; both
            # sides are already membership facts (diff --no-renames)
            renames.append(records[index])
            index += 1
            renames.append(path)
        tags: set[str] = set()
        if xy in _CONFLICT_CODES:
            tags.add(TAG_CONFLICTED)
            conflicts.append(path)
        if xy == '??':
            tags.add(TAG_UNTRACKED)
        else:
            # X = index side, Y = worktree side; anything other than
            # ' ' (unchanged) or '?' (absent) on a side means that
            # side holds a change -- one row, one coherent overlay
            if xy[0] not in ' ?':
                tags.add(TAG_STAGED)
            if xy[1] not in ' ?':
                tags.add(TAG_UNSTAGED)
            if 'D' in xy:
                tags.add(TAG_DELETED)
            if 'R' in xy or 'C' in xy:
                tags.add(TAG_RENAMED)
        states[path] = tuple(sorted(tags))
    return (dict(sorted(states.items())), tuple(sorted(set(conflicts))),
            tuple(sorted(set(renames))))


def snapshot(root: Path) -> WorkingState:
    """Build the one coherent snapshot for ``root``.

    Membership comes from the scope resolver's own discovery source
    (``changed_files``: base..worktree diff with rename detection off,
    plus untracked files), so auto-discovery and explicit targets
    describe the identical change set. State annotation and conflict
    detection come from a single ``git status --porcelain=v1 -z`` read.
    """
    from . import scope_resolver

    base = scope_resolver.default_base(root)
    paths = tuple(scope_resolver.changed_files(root, base))
    raw = _git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    states, conflicts, renames = _parse_porcelain_v1_z(raw)
    untracked = tuple(sorted(
        path for path, tags in states.items() if TAG_UNTRACKED in tags))
    # annotate membership rows that status did not list (e.g. paths
    # differing only by case): they are part of the snapshot as plain
    # worktree-diff facts with no extra tags
    merged = dict(states)
    for path in paths:
        merged.setdefault(path, ())
    for path in renames:
        merged[path] = tuple(sorted(set(merged.get(path, ())) |
                                    {TAG_RENAMED}))
    return WorkingState(root=root, base=base, paths=paths,
                        states=dict(sorted(merged.items())),
                        conflicts=conflicts, untracked=untracked)


def normalize_paths(root: Path, raw_paths: list[str]) -> list[str]:
    """Normalize explicit targets to repo-relative POSIX paths.

    Absolute paths under the repository are made relative, separators
    normalized, duplicates removed, ordering made deterministic (the
    same sorted order auto-discovery produces). Paths that resolve
    outside the repository are an operational error -- this is path
    hygiene, not a governance rule.
    """
    root = root.resolve()
    normalized: set[str] = set()
    for raw in raw_paths:
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                rel = candidate.resolve().relative_to(root)
            except (ValueError, OSError):
                raise GitContextError(
                    'REPOSITORY_ERROR',
                    f'path outside the repository: {raw}') from None
        else:
            rel = (root / candidate).resolve()
            try:
                rel = rel.relative_to(root)
            except (ValueError, OSError):
                raise GitContextError(
                    'REPOSITORY_ERROR',
                    f'path outside the repository: {raw}') from None
        normalized.add(rel.as_posix())
    return sorted(normalized)
