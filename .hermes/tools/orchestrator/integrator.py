#!/usr/bin/env python3
"""Governed tree integration: atomically apply sealed worker results
onto the target branch under the golden invariant.

    PASS(A) + PASS(B) != PASS(A U B)

Every `--apply` runs an integration gate on the UNIFIED candidate tree
before anything reaches the target branch. Any failure (merge conflict,
gate failure, debris, commit error) rolls the target all the way back to
the pre-apply HEAD: all-or-nothing, zero debris, never broken code on
the branch. Evidence is re-verified cryptographically BEFORE the first
index mutation: tampered or missing evidence aborts with the tree
byte-identical to where it started.
"""
from __future__ import annotations

import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .types import TAIL_CHARS, OrchestratorError
from .worktree import _git

# Integration gate default: run the candidate tree's test suite through
# the same interpreter that runs the orchestrator.
DEFAULT_VERIFY_ARGS = ('-m', 'pytest', 'tests/', '-q')


@dataclass(frozen=True)
class IntegrationResult:
    """Audit record of one apply attempt; rides the run report as
    `report['integration']`.

    status vocabulary: applied | conflict | verification_failed |
    invalid_evidence | refused.
    """

    status: str
    error: str | None = None
    commit_sha: str | None = None
    workers: tuple[str, ...] = ()
    evidence_shas: tuple[str, ...] = ()
    generation: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            'status': self.status, 'error': self.error,
            'commit_sha': self.commit_sha, 'workers': list(self.workers),
            'evidence_shas': list(self.evidence_shas),
            'generation': self.generation,
        }


class TreeIntegrator:
    """Atomically merge verified worker results into the target repo.

    Inputs: target repo path, sealed evidence files of completed
    workers, optional integration-verification command (defaults to the
    candidate tree's test suite), and the run's graph generation for the
    structured audit message.
    """

    def __init__(self, repo: Path, evidences: list[Path], *,
                 verify_cmd: list[str] | None = None,
                 generation: int | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.evidences = [Path(path) for path in evidences]
        self.verify_cmd = verify_cmd
        self.generation = generation

    # -- pre-flight: every check that must hold BEFORE any mutation ----

    def _preflight(self) -> tuple[str | None, list[tuple[str, str, str]],
                                  str | None]:
        """Return (base_sha, [(worker, commit, evidence_sha)...], error).

        Order matters: cleanliness and full evidence re-verification all
        happen while the index and worktree are still untouched.
        """
        try:
            dirty = _git(self.repo, 'status', '--porcelain')
        except OrchestratorError as exc:
            return None, [], f'worktree status unreadable: {exc}'
        if dirty:
            return None, [], 'worktree not clean before apply:\n' + dirty
        base = _git(self.repo, 'rev-parse', 'HEAD')
        # Deferred import: scheduler imports this module at package
        # init; the evidence verifier lives there, so the reverse edge
        # must stay lazy to keep the package import cycle-free.
        from .scheduler import verify_worker_evidence
        picks: list[tuple[str, str, str]] = []
        for path in self.evidences:
            if not path.is_file():
                return None, [], f'missing evidence file: {path}'
            try:
                payload = verify_worker_evidence(path)
            except OrchestratorError as exc:
                return None, [], f'invalid evidence {path.name}: {exc}'
            wid = str(payload.get('worker_id'))
            commit = str(payload.get('commit'))
            target = str(payload.get('target_tree_sha'))
            if payload.get('base_commit') != base:
                return None, [], (
                    f'{wid}: evidence base {payload.get("base_commit")} '
                    f'!= target HEAD {base}')
            try:
                _git(self.repo, 'cat-file', '-e', f'{commit}^{{object}}')
                actual = _git(self.repo, 'rev-parse', f'{commit}^{{tree}}')
            except OrchestratorError:
                return None, [], (
                    f'{wid}: evidence commit unreadable in object store: '
                    f'{commit}')
            if actual != target:
                return None, [], (
                    f'{wid}: target_tree_sha {target} != commit tree '
                    f'{actual}')
            picks.append((wid, commit,
                          str(payload.get('evidence_sha256'))))
        if not picks:
            return None, [], 'no evidence to integrate'
        picks.sort(key=lambda item: item[0])  # deterministic merge order
        return base, picks, None

    # -- rollback: return the repo to the exact pre-apply state --------

    def _rollback(self, base: str) -> str | None:
        """Clear any sequencer/conflict state, restore HEAD, drop debris.
        Returns a note when the tree is STILL not pristine (never
        silent), or None when rollback verifiably succeeded."""
        for args in (('cherry-pick', '--quit'), ('reset', '--hard', base),
                     ('clean', '-fd')):
            with suppress(OrchestratorError):
                _git(self.repo, *args)
        try:
            leftover = _git(self.repo, 'status', '--porcelain')
            head = _git(self.repo, 'rev-parse', 'HEAD')
        except OrchestratorError as exc:
            return f'rollback status unreadable: {exc}'
        if leftover:
            return 'rollback left debris:\n' + leftover
        if head != base:
            return f'rollback did not restore HEAD ({head} != {base})'
        return None

    def _fail(self, status: str, error: str, base: str,
              picks: list[tuple[str, str, str]]) -> IntegrationResult:
        note = self._rollback(base)
        if note:
            error = f'{error}\n[rollback] {note}'
        return IntegrationResult(
            status=status, error=error,
            workers=tuple(wid for wid, _, _ in picks),
            evidence_shas=tuple(sha for _, _, sha in picks),
            generation=self.generation)

    # -- the apply itself ----------------------------------------------

    def apply(self) -> IntegrationResult:
        base, picks, error = self._preflight()
        if error is not None or base is None:
            # Rejected before ANY index/worktree mutation (1.4).
            return IntegrationResult(status='invalid_evidence', error=error)

        # Stage every worker result onto the candidate tree, no commits.
        # Captured directly (no parsing): git prints `CONFLICT (...)`
        # diagnostics on stdout, which a stderr-only error would drop.
        try:
            proc = subprocess.run(
                ['git', 'cherry-pick', '--no-commit',
                 *[commit for _, commit, _ in picks]],
                cwd=self.repo, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return self._fail('conflict', f'{type(exc).__name__}: {exc}',
                              base, picks)
        if proc.returncode != 0:
            detail = ((proc.stdout or '') + (proc.stderr or '')).strip()
            return self._fail('conflict',
                              detail or f'cherry-pick exit {proc.returncode}',
                              base, picks)

        # The golden invariant gate: the UNIFIED tree must prove itself.
        cmd = (list(self.verify_cmd) if self.verify_cmd
               else [sys.executable, *DEFAULT_VERIFY_ARGS])
        try:
            proc = subprocess.run(cmd, cwd=self.repo, capture_output=True,
                                  text=True, timeout=900)
            gate_out = (proc.stdout or '') + (proc.stderr or '')
            gate_ok = proc.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            gate_out = f'{type(exc).__name__}: {exc}'
            gate_ok = False  # a gate that cannot run never passes
        if not gate_ok:
            return self._fail('verification_failed',
                              gate_out[-TAIL_CHARS:], base, picks)

        # Distinguish expected staged state from verify-stage debris
        # (untracked files or unstaged edits) before committing.
        porcelain = _git(self.repo, 'status', '--porcelain')
        debris = [line for line in porcelain.splitlines()
                  if line.startswith('??') or line[1:2] not in ('', ' ')]
        if debris:
            return self._fail('refused',
                              'integration debris: ' + ', '.join(debris),
                              base, picks)

        workers = tuple(wid for wid, _, _ in picks)
        message = (
            f'asha-apply: atomic integration of {len(picks)} worker(s)\n\n'
            f'workers: {", ".join(workers)}\n'
            f'evidence: {", ".join(sha[:16] for _, _, sha in picks)}\n'
            f'generation: {self.generation}')
        try:
            _git(self.repo, '-c', 'user.name=Asha Orchestrator',
                 '-c', 'user.email=orchestrator@asha.local',
                 'commit', '-q', '-m', message)
            commit_sha = _git(self.repo, 'rev-parse', 'HEAD')
        except OrchestratorError as exc:
            return self._fail('refused', f'commit failed: {exc}',
                              base, picks)
        if _git(self.repo, 'status', '--porcelain'):
            return self._fail('refused',
                              'post-commit debris on target branch',
                              base, picks)
        return IntegrationResult(
            status='applied', commit_sha=commit_sha, workers=workers,
            evidence_shas=tuple(sha for _, _, sha in picks),
            generation=self.generation)
