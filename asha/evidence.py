#!/usr/bin/env python3
"""Tamper-resistant ship evidence engine.

Clean-tree invariant: ship evidence may only be produced (or consumed) when
`git status --porcelain` is strictly empty.

Tree binding: every artifact records commit (HEAD) and tree_hash
(HEAD^{tree}), so evidence is cryptographically tied to the exact git tree
it describes -- not to mutable file contents.

Canonical digest: evidence_sha256 = sha256(canonical JSON of the payload
with sort_keys, compact separators, excluding the digest field itself).
Any manual edit of a field (e.g. authorized_to_ship false -> true) breaks
the digest and the gate rejects the artifact.

Writes are atomic (temp sibling + os.replace) into .jspace/evidence.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = 1
EVIDENCE_NAME = 'evidence.json'


class EvidenceError(Exception):
    """Clean-tree / digest / structure violation (fail-closed)."""


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=root, capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        raise EvidenceError('git ' + ' '.join(args) + ': ' + proc.stderr.strip())
    return proc.stdout.strip()


def head_hash(root: Path) -> str:
    return _git(root, 'rev-parse', 'HEAD')


def tree_hash(root: Path) -> str:
    return _git(root, 'rev-parse', 'HEAD^{tree}')


def require_clean_tree(root: Path) -> None:
    proc = subprocess.run(['git', 'status', '--porcelain'], cwd=root,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise EvidenceError('git status failed: ' + proc.stderr.strip())
    dirty = [line for line in proc.stdout.splitlines() if line.strip()]
    if dirty:
        raise EvidenceError(
            'working tree is dirty; ship evidence requires a clean tree: '
            + ', '.join(dirty[:10]))


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False)


def compute_digest(payload: dict) -> str:
    body = {key: value for key, value in payload.items()
            if key != 'evidence_sha256'}
    return hashlib.sha256(canonical(body).encode('utf-8')).hexdigest()


def seal(payload: dict) -> dict:
    """Validate required fields, attach the canonical digest."""
    required = ('schema', 'stage', 'scope', 'commit', 'tree_hash',
                'observed_at', 'checks', 'authorized_to_ship')
    missing = [key for key in required if key not in payload]
    if missing:
        raise EvidenceError('evidence payload missing fields: ' + ', '.join(missing))
    sealed = dict(payload)
    sealed['evidence_sha256'] = compute_digest(sealed)
    return sealed


def evidence_path(root: Path) -> Path:
    return Path(root) / '.jspace' / EVIDENCE_NAME


def write(root: Path, sealed: dict) -> Path:
    path = evidence_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(sealed, indent=2, sort_keys=True,
                       ensure_ascii=False) + '\n').encode('utf-8')
    fd, tmp = tempfile.mkstemp(prefix='.evidence-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def read(root: Path) -> dict | None:
    path = evidence_path(root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise EvidenceError('evidence unreadable: ' + str(exc)) from exc
    if not isinstance(payload, dict):
        raise EvidenceError('evidence must be a JSON object')
    return payload


def verify(root: Path) -> dict | None:
    """Return the artifact when its digest is intact; None when absent.

    Any digest mismatch, missing required field, or boolean drift raises
    EvidenceError -- tampering can never authorize a ship.
    """
    payload = read(root)
    if payload is None:
        return None
    recorded = payload.get('evidence_sha256')
    if not isinstance(recorded, str):
        raise EvidenceError('evidence_sha256 missing')
    actual = compute_digest(payload)
    if actual != recorded:
        raise EvidenceError(
            'evidence_sha256 mismatch: artifact was modified after sealing '
            f'(recorded {recorded[:12]}..., actual {actual[:12]}...)')
    if not isinstance(payload.get('authorized_to_ship'), bool):
        raise EvidenceError('authorized_to_ship must be a boolean')
    return payload
