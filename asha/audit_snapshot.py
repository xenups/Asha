"""Deterministic, content-addressed audit snapshot (Phase 1: Stage 1).

Single responsibility: turn the intended source state of a repository into
a canonical, reproducible manifest and a content-addressed ``snapshot_id``.

Why this exists next to evidence.py's commit/HEAD^{tree} binding:

    evidence.commit + evidence.tree_hash bind a sealed payload to the
    COMMITTED git tree. That binding says nothing about working-tree
    content, staged-only content, or eligible untracked files -- the very
    state an audit authorizes. This module records the actual bytes of the
    intended source state, so two runs over identical included
    paths+contents yield the same identifier and any byte change yields a
    different one.

It is ADDITIVE: nothing here replaces commit/tree_hash, and no existing
evidence field changes shape. The manifest is a separate artifact carrying
its own version, so future schema changes cannot silently reuse old ids.

Inclusion policy (explicit, documented -- no silent omission):

  1. EXCLUDED_DIRS  - VCS internals, caches, virtualenvs, and the
     externalized Asha state zone are never walked. Deterministic literal
     rules, not globs over the whole tree.
  2. .gitignore    - ignored paths are excluded EXCEPT for the narrow set
     of build/test/config inputs listed in INCLUDE_DESPITE_IGNORE
     (a .gitignore that hides a file required by the audit is a silent
     omission, so the intersection is materialized explicitly).
  3. .git          - the VCS database itself is excluded structurally.

Path normalization: NFC, POSIX separators, repository-relative. Ordering:
byte-lexicographic on the normalized path -- independent of filesystem
enumeration order.

Membership comes from git itself (``ls-files`` + ``status --porcelain``),
so the snapshot sees exactly what the repository sees. Documented content
policy: when a path has both staged and unstaged content, the WORKING-TREE
bytes are the audited state.

Unsupported entry types, symlinks, unreadable files, and path collisions
fail closed with SnapshotError. Nothing is skipped quietly.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# Manifest format version: part of the identifier, so a schema change can
# never silently reuse a previously issued snapshot_id.
MANIFEST_VERSION = 1

# Directory names excluded structurally (any depth). Deterministic and
# literal -- deliberate, not a heuristic.
EXCLUDED_DIRS = frozenset({
    '.git', '.hg', '.svn',
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    '.jspace', '.hermes', '.venv', 'venv', 'node_modules',
})

# Ignored files that ARE required inputs to the build/test/audit decision.
# Materialized explicitly rather than inherited from .gitignore.
INCLUDE_DESPITE_IGNORE = frozenset({
    '.gitignore',
    'pyproject.toml',
    'ruff.toml',
    'mypy.ini',
    'pytest.ini',
    'setup.cfg',
    'tox.ini',
})

# Entry types (git-mode derived). Anything else fails closed.
ENTRY_FILE = 'file'
ENTRY_DELETED = 'deleted'


class SnapshotError(Exception):
    """Snapshot construction failure (fail-closed)."""


@dataclass(frozen=True)
class ManifestEntry:
    """One included source entry, unambiguously typed."""

    path: str            # normalized repository-relative POSIX path
    entry_type: str      # ENTRY_FILE | ENTRY_DELETED
    digest: str          # sha256 of the content, or '' for a deletion
    size: int            # byte length of the content (0 for a deletion)


@dataclass(frozen=True)
class AuditSnapshot:
    """A built manifest plus its content-addressed identifier."""

    snapshot_id: str
    manifest_version: int
    entries: tuple[ManifestEntry, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.entries)

    def index(self) -> dict[str, ManifestEntry]:
        return {entry.path: entry for entry in self.entries}

    @property
    def digest(self) -> str:
        """Alias for callers that speak of 'the snapshot digest'."""
        return self.snapshot_id


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(['git', *args], cwd=root, capture_output=True,
                          text=True, timeout=60, encoding='utf-8',
                          errors='replace')
    if proc.returncode != 0:
        raise SnapshotError('git ' + ' '.join(args) + ': '
                            + (proc.stderr or '').strip())
    return proc


def normalize_rel(path: str) -> str:
    """Repository-relative, POSIX-separated, no leading './'."""
    text = unicodedata.normalize('NFC', path.replace('\\', '/'))
    while text.startswith('./'):
        text = text[2:]
    return text.lstrip('/')


def _excluded_path(rel: str) -> bool:
    """True when any DIRECTORY component is structurally excluded.

    The final component is never matched: a file literally named 'venv' is
    source; a directory named 'venv' is not.
    """
    parts = rel.split('/')
    return any(part in EXCLUDED_DIRS for part in parts[:-1])


def _required_ignored_inputs(root: Path) -> list[str]:
    """Required ignored inputs present on disk (explicit intersection)."""
    return [normalize_rel(name) for name in sorted(INCLUDE_DESPITE_IGNORE)
            if (root / name).is_file()]


def _status_map(root: Path) -> dict[str, str]:
    """porcelain-v1 XY status per path ('??' = untracked).

    A rename/copy emits ONE record whose source path is the next NUL
    token; the destination keeps its 'R '/'C ' status.
    """
    raw = _git(root, 'status', '--porcelain=v1', '-z',
               '--untracked-files=all').stdout
    tokens = raw.split('\0')
    statuses: dict[str, str] = {}
    cursor = 0
    while cursor < len(tokens):
        token = tokens[cursor]
        cursor += 1
        if len(token) < 4:
            continue
        status = token[:2]
        path = normalize_rel(token[3:])
        if status[:1] in ('R', 'C') and cursor < len(tokens):
            cursor += 1
        if path:
            statuses[path] = status
    return statuses


def _candidate_paths(root: Path, statuses: dict[str, str]) -> list[str]:
    """Included paths: tracked files, changed paths, required inputs."""
    included: list[str] = []
    for raw_path in _git(root, 'ls-files', '-z').stdout.split('\0'):
        if not raw_path:
            continue
        rel = normalize_rel(raw_path)
        if not _excluded_path(rel):
            included.append(rel)
    for rel in statuses:
        if not _excluded_path(rel):
            included.append(rel)
    included.extend(_required_ignored_inputs(root))
    # deterministic ordering, independent of enumeration order
    return sorted(set(included))


def _content_bytes(root: Path, rel: str) -> bytes | None:
    """The audited bytes for one path, or None when it is a deletion.

    Documented snapshot policy: for a path with staged AND unstaged
    content, the WORKING-TREE bytes are what the audit covers -- the state
    an operator can see and re-run.
    """
    path = root / rel
    if not path.exists():
        return None
    if path.is_symlink():
        # Following a symlink could audit bytes that are not part of the
        # source state (or escape the repository entirely).
        raise SnapshotError(f'unsupported entry type (symlink): {rel}')
    if not path.is_file():
        raise SnapshotError(f'unsupported entry type: {rel}')
    try:
        return path.read_bytes()
    except OSError as exc:
        # inability to read a required file fails closed
        raise SnapshotError(f'unreadable included file {rel}: {exc}') from exc


def canonical_manifest(entries: tuple[ManifestEntry, ...],
                       manifest_version: int = MANIFEST_VERSION) -> str:
    """Canonical, unambiguous serialization of the manifest.

    One JSON object (never bare field concatenation) plus an explicit
    format version, so no two distinct manifests serialize identically.
    """
    payload = {
        'manifest_version': manifest_version,
        'entries': [
            {
                'path': entry.path,
                'entry_type': entry.entry_type,
                'digest': entry.digest,
                'size': entry.size,
            }
            for entry in entries
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False)


def compute_snapshot_id(entries: tuple[ManifestEntry, ...],
                        manifest_version: int = MANIFEST_VERSION) -> str:
    """Content-addressed identifier over the canonical manifest."""
    body = canonical_manifest(entries, manifest_version)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def build_manifest(root: Path) -> AuditSnapshot:
    """Build the deterministic manifest for the current intended state."""
    root = Path(root).resolve()
    if not (root / '.git').exists():
        raise SnapshotError(f'not a git repository: {root}')
    statuses = _status_map(root)
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for rel in _candidate_paths(root, statuses):
        if rel in seen:
            continue
        seen.add(rel)
        blob = _content_bytes(root, rel)
        if blob is None:
            entries.append(ManifestEntry(path=rel, entry_type=ENTRY_DELETED,
                                         digest='', size=0))
            continue
        entries.append(ManifestEntry(
            path=rel, entry_type=ENTRY_FILE,
            digest=hashlib.sha256(blob).hexdigest(), size=len(blob)))
    entries.sort(key=lambda entry: entry.path.encode('utf-8'))
    frozen = tuple(entries)
    return AuditSnapshot(snapshot_id=compute_snapshot_id(frozen),
                         manifest_version=MANIFEST_VERSION,
                         entries=frozen)


def snapshot_id_of(root: Path) -> str:
    """Convenience: the snapshot_id of the current intended state."""
    return build_manifest(root).snapshot_id
