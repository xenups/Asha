#!/usr/bin/env python3
"""Dispatch-safety primitives: structural path coverage, declared-scope
status, and the read/write collision matrix (UNKNOWN != SAFE).

Conflict state never mutates the dependency graph: a deferred node
stays dependency-ready, only not currently dispatchable."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_PAIR_LABELS = {'ww': 'write_write', 'wr': 'write_read',
                'rw': 'read_write'}


# ---------------------------------------------------------------------------
# Structural path primitives (file/module granularity only; no semantic
# precision is claimed beyond what these rules establish).
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    return path.replace('\\', '/').rstrip('/')


def _has_wild(entry: str) -> bool:
    return '*' in entry or '?' in entry


def _glob_match(path: str, pattern: str) -> bool:
    """Strict structural glob: '*' and '?' never cross '/', '**' does."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '*':
            if pattern[i:i + 2] == '**':
                out.append('.*')
                i += 2
                continue
            out.append('[^/]*')
        elif ch == '?':
            out.append('[^/]')
        else:
            out.append(re.escape(ch))
        i += 1
    return re.fullmatch(''.join(out), path) is not None


def covered(path: str, entry: str) -> bool:
    """True when `entry` (literal, directory-style or strict glob) provably
    covers the concrete `path`. Directory entries cover children only at a
    path-component boundary (a file can never have children)."""
    path, entry = _norm(path), _norm(entry)
    if path == entry or path.startswith(entry + '/'):
        return True
    if _has_wild(entry):
        return _glob_match(path, entry)
    return False


def _literal_prefix(entry: str) -> str:
    return re.split(r'[*?]', entry, maxsplit=1)[0]


def overlap(left: str, right: str) -> bool:
    """Structural read/write overlap. Conservative: two wildcard entries
    are only declared disjoint when their literal prefixes separate them;
    anything unprovable counts as overlap (fail-closed, never silently
    downgraded to 'no overlap')."""
    lft, rgt = _norm(left), _norm(right)
    if covered(lft, rgt) or covered(rgt, lft):
        return True
    if _has_wild(lft) or _has_wild(rgt):
        lp, rp = _literal_prefix(lft), _literal_prefix(rgt)
        separable = bool(lp and rp and not lp.startswith(rp)
                         and not rp.startswith(lp))
        # literal prefixes separate them => proven disjoint; anything else
        # between two wildcard entries cannot be proven -> conflict.
        return not separable
    return False


# ---------------------------------------------------------------------------
# Pre-execution scope safety (known conditions only; no future prediction).
# ---------------------------------------------------------------------------

def scope_status(worker: dict[str, Any]) -> tuple[bool, str]:
    """Dispatch-time scope safety from the DECLARED scope alone. Unknown or
    structurally invalid declarations block the worker (UNKNOWN != SAFE);
    nothing here predicts what the worker will actually change."""
    declared = worker.get('declared_scope')
    if declared is None:
        return False, 'unknown_declared_scope'
    if not isinstance(declared, list) or not declared:
        return False, 'empty_declared_scope'
    for entry in declared:
        if not isinstance(entry, str) or not entry.strip():
            return False, 'invalid_declared_scope_entry:' + repr(entry)
        parts = _norm(entry).split('/')
        if Path(entry).is_absolute() or entry.startswith('/') or '..' in parts:
            return False, 'invalid_declared_scope_entry:' + repr(entry)
    return True, 'declared_scope_ok'


# ---------------------------------------------------------------------------
# 6.2 ConflictManager -- pure read/write comparison over running workers.
# Conflict state never mutates the dependency graph: a deferred node stays
# dependency-ready, it is simply not currently dispatchable.
# ---------------------------------------------------------------------------

def _intersection(label: str, left: Any, right: Any) -> str | None:
    """None = intersection proven empty. UNKNOWN (None) on either side is
    detected BEFORE the known-empty shortcut: UNKNOWN x known-empty defers
    too -- UNKNOWN != SAFE, absence of evidence never collapses with
    evidence of absence."""
    if left is None or right is None:
        return f'{label}_unknown_set'
    if left == [] or right == []:
        return None  # known-empty side => intersection provably empty
    for litem in left:
        for ritem in right:
            if overlap(str(litem), str(ritem)):
                return f'{label}_overlap: {litem} ~ {ritem}'
    return None


def _pair_conflict(cand: dict[str, Any],
                   other: dict[str, Any]) -> str | None:
    """Unsafe overlap: cand.write&other.write, cand.write&other.read,
    other.write&cand.read. Read/read never conflicts."""
    ww = _intersection(_PAIR_LABELS['ww'], cand['writes'], other['writes'])
    if ww:
        return ww
    wr = _intersection(_PAIR_LABELS['wr'], cand['writes'], other['reads'])
    if wr:
        return wr
    rw = _intersection(_PAIR_LABELS['rw'], other['writes'], cand['reads'])
    if rw:
        return rw
    return None


class ConflictManager:
    """Compares dependency-ready candidates against currently running
    workers using only KNOWN read/write information."""

    def __init__(self) -> None:
        self._running: dict[str, dict[str, Any]] = {}

    @property
    def running(self) -> tuple[str, ...]:
        return tuple(sorted(self._running))

    def start(self, worker_id: str, worker: dict[str, Any]) -> None:
        self._running[worker_id] = worker

    def finish(self, worker_id: str) -> None:
        self._running.pop(worker_id, None)

    def assess(self, worker: dict[str, Any]) -> tuple[bool, str]:
        for other_id in sorted(self._running):
            reason = _pair_conflict(worker, self._running[other_id])
            if reason:
                return False, f'{other_id}: {reason}'
        return True, 'proven_disjoint'
