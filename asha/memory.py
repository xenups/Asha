#!/usr/bin/env python3
"""Institutional memory layer: thin Mem0 adapter + context synthesis.

Architecture:
    ORIENT (project_map.py) = current repository ground truth
    Mem0 (this module)      = historical / institutional memory, ADVISORY
    precedence (mandatory): CURRENT REPOSITORY FACTS > STORED MEMORY

A retrieved memory never overrides a conflicting current repository fact;
conflicting repository facts are marked ``stale`` (never deleted).

Asha does not require Mem0: every operation fails closed through
``MemoryLayerError`` while ORIENT / SCOPE / GATE keep working.

Observed mem0ai 2.1.0 API (recorded from the installed package):
    add(messages, *, user_id, metadata, infer=False, ...)  -> results[]
    search(query, *, top_k, filters)                       -> {"results": [...]}
    get_all(*, filters)                                    -> {"results": [...]}
    update(memory_id, text=None, metadata=None)
    delete(memory_id)

Offline profile (no API keys, no daemon, no LLM call):
    vector store = chroma under <root>/.jspace/cache/mem0 (git-ignored)
    embedder     = mem0's own MockEmbeddings (registered via factory map;
                   mem0's pydantic validator has no "mock" entry, so the
                   config instance is built with model_construct)
    llm          = openai provider with a dummy key, never invoked because
                   every add uses infer=False
    ponytail: with a real OPENAI_API_KEY swap embedder/llm providers for
    semantic ranking; offline relevance is enforced by the deterministic
    relevance gate (evaluate_candidate/select_memory in search_memory):
    Mem0 retrieves candidates, Asha selects bounded advisory context.
"""
from __future__ import annotations

import sys

if __package__ in (None, ""):
    # Direct-script mode: drop this directory from sys.path BEFORE any
    # stdlib import -- the sibling types.py would otherwise shadow stdlib
    # `types` and kill the import chain on GenericAlias (same guard as
    # asha/__main__.py and asha/mcp_server.py).
    def _asha_norm(entry: str) -> str:
        return (entry or ".").replace(chr(92), "/").rstrip("/").lower()

    _asha_pkg = _asha_norm(__file__).rsplit("/", 1)[0]
    sys.path = [entry for entry in sys.path if _asha_norm(entry) != _asha_pkg]
    sys.path.insert(0, _asha_pkg.rsplit("/", 1)[0])

import argparse
import json
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = 1
CATEGORIES = ('repository_fact', 'workflow_preference', 'historical_lesson',
              'decision_record')
TREE_SENSITIVE = frozenset({'repository_fact'})  # STEP 7: only these are
                                                 # revalidated vs ORIENT
_OFFLINE_DUMMY_LLM_KEY = 'asha-offline-never-called'

# STEP 11: refuse obvious secret-like content at the trust boundary.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('private key block',
     re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----', re.IGNORECASE)),
    ('AWS access key id', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('GitHub token', re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b'
                                r'|\bgithub_pat_[A-Za-z0-9_]{22,}\b')),
    ('OpenAI-style key', re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b')),
    ('Slack token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b')),
    ('JWT', re.compile(r'\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.')),
    ('inline credential',
     re.compile(r'\b(?:password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*'
                r'\S{6,}', re.IGNORECASE)),
)


class MemoryLayerError(RuntimeError):
    """All memory failures surface as this; callers fail closed."""


def _secret_kind(text: str) -> str | None:
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(['git', *args], cwd=root, capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ''
    return proc.stdout.strip() if proc.returncode == 0 else ''


def repo_identity(root: Path) -> str:
    """Unambiguous repo id: remote URL, else absolute path (never the bare
    repository name). Identity lookup only -- analysis stays in
    project_map.py / scope_resolver.py."""
    remote = _git(root, 'config', '--get', 'remote.origin.url')
    if remote:
        return 'remote:' + remote
    return 'path:' + str(root.resolve())


def current_tree_hash(root: Path) -> str | None:
    return _git(root, 'rev-parse', 'HEAD^{tree}') or None


# --------------------------------------------------------------------------
# Backend boundary (STEP 3): Mem0 isolated behind these four duck-typed
# methods. NullBackend = Mem0 unavailable; every call raises, nothing
# degrades silently.
# --------------------------------------------------------------------------

class NullBackend:
    available = False

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _fail(self, op: str) -> None:
        raise MemoryLayerError(
            f'MEMORY UNAVAILABLE ({op}): {self.reason}')

    def add(self, content: str, *, user_id: str,
            metadata: dict[str, Any]) -> str:
        self._fail('add')
        raise AssertionError  # unreachable

    def search(self, query: str, *, user_id: str,
               top_k: int) -> list[dict[str, Any]]:
        self._fail('search')
        raise AssertionError

    def get_all(self, *, user_id: str) -> list[dict[str, Any]]:
        self._fail('get_all')
        raise AssertionError

    def update(self, memory_id: str, *, text: str | None = None,
               metadata: dict[str, Any] | None = None) -> None:
        self._fail('update')
        raise AssertionError

    def delete(self, memory_id: str) -> None:
        self._fail('delete')
        raise AssertionError


def _normalise(items: Any) -> list[dict[str, Any]]:
    if isinstance(items, dict):
        items = items.get('results', [])
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append({
            'id': str(item.get('id', '')),
            'content': item.get('memory') or item.get('content')
            or item.get('text') or '',
            'metadata': dict(item.get('metadata') or {}),
            'created_at': item.get('created_at'),
            'updated_at': item.get('updated_at'),
            'user_id': item.get('user_id'),
        })
    return out


class Mem0Backend:
    available = True
    reason = None

    def __init__(self, root: Path) -> None:
        from mem0 import Memory
        from mem0.configs.base import MemoryConfig
        from mem0.embeddings.configs import EmbedderConfig
        from mem0.utils.factory import EmbedderFactory

        cache = root / '.jspace' / 'cache' / 'mem0'
        cache.mkdir(parents=True, exist_ok=True)
        # mem0 ships MockEmbeddings but does not register it as a provider;
        # extend the factory map (uses mem0's own class, no new dependency).
        EmbedderFactory.provider_to_class.setdefault(
            'mock', 'mem0.embeddings.mock.MockEmbeddings')
        # model_construct bypasses mem0's provider whitelist validator for
        # 'mock' -- see module docstring "Offline profile".
        embedder = EmbedderConfig.model_construct(provider='mock', config={})
        config = MemoryConfig(
            vector_store={'provider': 'chroma', 'config': {
                'collection_name': 'asha_memory',
                'path': str(cache / 'chroma')}},
            llm={'provider': 'openai', 'config': {
                'api_key': _OFFLINE_DUMMY_LLM_KEY}},
            embedder=embedder,
            history_db_path=str(cache / 'history.db'),
        )
        self._m = Memory(config)

    def add(self, content: str, *, user_id: str,
            metadata: dict[str, Any]) -> str:
        result = self._m.add(content, user_id=user_id, metadata=metadata,
                             infer=False)
        items = _normalise(result)
        return items[0]['id'] if items else str(uuid.uuid4())

    def search(self, query: str, *, user_id: str,
               top_k: int) -> list[dict[str, Any]]:
        raw = self._m.search(query, filters={'user_id': user_id},
                             top_k=top_k)
        return _normalise(raw)

    def get_all(self, *, user_id: str) -> list[dict[str, Any]]:
        raw = self._m.get_all(filters={'user_id': user_id}, top_k=100)
        return _normalise(raw)

    def update(self, memory_id: str, *, text: str | None = None,
               metadata: dict[str, Any] | None = None) -> None:
        self._m.update(memory_id, text=text, metadata=metadata)

    def delete(self, memory_id: str) -> None:
        self._m.delete(memory_id)


_OVERRIDE: Any | None = None


def set_backend(backend: Any | None) -> None:
    """Adapter-boundary injection for tests / demos (None = clear)."""
    global _OVERRIDE
    _OVERRIDE = backend


def resolve_backend(root: Path) -> Any:
    if _OVERRIDE is not None:
        return _OVERRIDE
    try:
        return Mem0Backend(root)
    except Exception as exc:
        #                           never a crash: memory is optional.
        return NullBackend(f'{type(exc).__name__}: {exc}')


# --------------------------------------------------------------------------
# STEP 3/4: typed public interface
# --------------------------------------------------------------------------

def add_memory(root: Path, content: str, category: str, *,
               source: str = 'agent', fact_key: str | None = None,
               fact_value: str | None = None,
               tree_hash: str | None = None,
               backend: Any | None = None) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise MemoryLayerError(
            f'invalid category {category!r}: one of {CATEGORIES}')
    secret = _secret_kind(content)
    if secret:
        raise MemoryLayerError(
            f'REFUSED: secret-like content ({secret}) not persisted')
    if fact_key is not None and category not in TREE_SENSITIVE:
        raise MemoryLayerError(
            'fact_key only applies to repository_fact memories')
    store = backend if backend is not None else resolve_backend(root)
    repo_id = repo_identity(root)
    metadata: dict[str, Any] = {
        'category': category,
        'source': source,
        'repo_id': repo_id,
        'recorded_at': _utc_now(),
        'status': 'active',
    }
    if category in TREE_SENSITIVE:
        # bind repository-state memory to the observed tree when available;
        # workflow/lessons/decisions are never artificially tree-bound.
        metadata['tree_hash'] = tree_hash or current_tree_hash(root)
    if fact_key is not None:
        metadata['fact_key'] = fact_key
        metadata['fact_value'] = fact_value
    memory_id = store.add(content, user_id=repo_id, metadata=metadata)
    return {'id': memory_id, 'content': content, 'metadata': metadata}


# --------------------------------------------------------------------------
# Deterministic relevance gate (retrieval is not selection):
#     Mem0 candidate retrieval -> relevance gate -> current-fact conflict
#     gate -> bounded top-k -> agent.  Authority model stays immutable:
#     CURRENT REPOSITORY FACTS > STORED MEMORY.  No new model, embedding
#     system or daemon: cheap token/entity matching over candidates only.
# --------------------------------------------------------------------------

GENERIC_TOKENS = frozenset({
    'file', 'files', 'test', 'tests', 'code', 'project', 'module', 'service',
    'data', 'function', 'functions', 'class', 'classes', 'change', 'error',
    'errors', 'bug', 'fix', 'issue', 'task', 'work', 'make', 'need', 'use',
    'update', 'add', 'remove', 'run', 'new', 'old',
})
ACCEPT_REASONS = frozenset({
    'target_path_match', 'target_symbol_match', 'multi_token_overlap',
    'task_token_match',
})
REJECT_REASONS = frozenset({
    'repo_mismatch', 'stale_repository_fact', 'current_fact_override',
    'repository_fact_without_entity_anchor', 'generic_token_only',
    'no_task_overlap', 'duplicate_content', 'top_k_bound',
})
_REASON_RANK = {'target_path_match': 4, 'target_symbol_match': 4,
                'multi_token_overlap': 3, 'task_token_match': 2}
DEFAULT_TOP_K = 3
_PATH_RE = re.compile(
    r'[\w./\\-]+\.(?:py|md|json|ya?ml|toml|sh|ts|js|sql|cfg|ini|txt)\b')
_CAMEL_RE = re.compile(r'\b[A-Za-z0-9]+(?:[A-Z][a-z0-9]+)+\b')
_SNAKE_RE = re.compile(r'\b[a-z0-9]+_[a-z0-9_]+\b')


def _norm_tokens(text: str) -> set[str]:
    """Lower alnum tokens minus generic tokens: generic overlap alone can
    never be a sufficient relevance signal."""
    return {tok for tok in re.findall(r'[a-z0-9]+', text.lower())
            if tok not in GENERIC_TOKENS and len(tok) > 1}


def _squash(text: str) -> str:
    """bundle_writer.py / bundle-writer / BundleWriter -> bundlewriter(+py):
    deterministic separator/case-insensitive comparison, no fuzzy matcher."""
    return re.sub(r'[^a-z0-9]', '', text.lower())


def _task_entities(task: str) -> list[tuple[str, str]]:
    """(kind, entity) pairs read straight from the task text: file paths
    and CamelCase / snake_case identifiers. No AST scan, no deep ORIENT."""
    entities: list[tuple[str, str]] = []
    for match in _PATH_RE.findall(task):
        entities.append(('path', match))
    for match in _CAMEL_RE.findall(task):
        entities.append(('symbol', match))
    for match in _SNAKE_RE.findall(task):
        entities.append(('symbol', match))
    return entities


def _reject(record: dict[str, Any], reason: str) -> dict[str, Any]:
    assert reason in REJECT_REASONS, reason  # decisions stay enumerable
    return {'memory_id': record.get('id', ''),
            'category': record.get('metadata', {}).get('category'),
            'accepted': False, 'reason': reason, 'matched_entities': []}


def evaluate_candidate(record: dict[str, Any], *, task: str, repo_id: str,
                       orient: dict[str, Any] | None = None) -> dict[str, Any]:
    """One deterministic, inspectable accept/reject decision per candidate.

    Hard rejects (in order): repo identity mismatch; explicitly stale
    repository fact (historical lessons are never killed by staleness);
    conflict with a current ORIENT fact.  Strong positives: exact target
    path / symbol.  Medium: >= 2 meaningful task-token overlaps (a
    repository_fact needs an anchor of one of these two kinds).  Generic
    token overlap alone -> rejected as generic_token_only.
    """
    meta = record.get('metadata', {})
    category = str(meta.get('category', ''))
    # hard reject 1 -- repository identity.  A missing repo_id is not a
    # mismatch: the backend already scopes the fetch to user_id == repo_id.
    stored_repo = meta.get('repo_id')
    if stored_repo is not None and stored_repo != repo_id:
        return _reject(record, 'repo_mismatch')
    # hard reject 2 -- stale repository facts stay stored/searchable but
    # never re-enter advisory context; lessons are category-scoped here.
    if category == 'repository_fact' and meta.get('status') == 'stale':
        return _reject(record, 'stale_repository_fact')
    # hard reject 3 -- current-fact conflict gate (facts > memory).
    if orient is not None and category == 'repository_fact':
        fact_key = meta.get('fact_key')
        if fact_key:
            current = _resolve_current(orient, str(fact_key))
            if meta.get('fact_value') != current:
                return _reject(record, 'current_fact_override')
    haystack = (f"{record.get('content', '')} {meta.get('fact_key', '')} "
                f"{meta.get('fact_value', '')}")
    hay_squash = _squash(haystack)
    task_norm = _norm_tokens(task)
    hay_norm = _norm_tokens(haystack)
    entities = _task_entities(task)
    matched_paths = [entity for kind, entity in entities
                     if kind == 'path' and _squash(entity) in hay_squash]
    matched_stems = [entity for kind, entity in entities
                     if kind == 'path'
                     and len(_squash(Path(entity).stem)) >= 5
                     and _squash(Path(entity).stem) in hay_squash]
    matched_symbols = [entity for kind, entity in entities
                       if kind == 'symbol' and len(_squash(entity)) >= 5
                       and _squash(entity) in hay_squash]
    overlap = sorted(task_norm & hay_norm)
    if matched_paths or matched_stems:
        reason, matched = 'target_path_match', matched_paths or matched_stems
    elif matched_symbols:
        reason, matched = 'target_symbol_match', matched_symbols
    elif category == 'repository_fact':
        # stronger anchor required: repo facts are the stale-prone class.
        if len(overlap) < 2:
            return _reject(record, 'repository_fact_without_entity_anchor')
        reason, matched = 'multi_token_overlap', overlap
    elif len(overlap) >= 2:
        reason, matched = 'multi_token_overlap', overlap
    elif len(overlap) == 1:
        reason, matched = 'task_token_match', overlap
    else:
        generic_shared = (set(re.findall(r'[a-z0-9]+', task.lower()))
                          & set(re.findall(r'[a-z0-9]+', haystack.lower())))
        return _reject(record, 'generic_token_only' if generic_shared
                       else 'no_task_overlap')
    decision = {'memory_id': record.get('id', ''), 'category': category,
                'accepted': True, 'reason': reason,
                'matched_entities': list(matched)[:8]}
    assert decision['reason'] in ACCEPT_REASONS
    return decision


def select_memory(records: list[dict[str, Any]], *, task: str, repo_id: str,
                   orient: dict[str, Any] | None = None,
                   top_k: int = DEFAULT_TOP_K) -> dict[str, Any]:
    """relevance gate -> conflict gate -> deterministic dedup -> bounded
    top-k.  Returns accepted records plus a full decision audit; only
    accepted records are agent-facing, the audit is for debugging/tests."""
    decisions = [evaluate_candidate(record, task=task, repo_id=repo_id,
                                    orient=orient) for record in records]
    by_id = {record['id']: record for record in records}
    position = {decision['memory_id']: index
                for index, decision in enumerate(decisions)}
    ranked = sorted(
        (decision for decision in decisions if decision['accepted']),
        key=lambda decision: (
            -_REASON_RANK[decision['reason']],
            str(by_id[decision['memory_id']]['metadata']
                .get('recorded_at') or ''),
            decision['memory_id']))
    seen_squash: dict[str, str] = {}
    accepted: list[dict[str, Any]] = []
    for decision in ranked:
        record = by_id[decision['memory_id']]
        squash = _squash(str(record.get('content', '')))
        if squash in seen_squash:
            # near-identical text: expose one representative only
            # (deterministic squash dedup; no clustering infrastructure).
            decisions[position[decision['memory_id']]] = {
                **decision, 'accepted': False, 'reason': 'duplicate_content',
                'duplicate_of': seen_squash[squash]}
            continue
        if len(accepted) >= top_k:
            # bounded exposure: never dump all candidates into context.
            decisions[position[decision['memory_id']]] = {
                **decision, 'accepted': False, 'reason': 'top_k_bound'}
            continue
        seen_squash[squash] = decision['memory_id']
        accepted.append(record)
    return {'accepted': accepted, 'decisions': decisions}


def _fetch_candidates(root: Path, task: str, *, limit: int,
                      backend: Any | None) -> tuple[Any, str,
                                                    list[dict[str, Any]]]:
    store = backend if backend is not None else resolve_backend(root)
    repo_id = repo_identity(root)
    # candidate window stays small and cheap (STEP 16); selection,
    # not window size, is what bounds what the agent sees.
    raw = store.search(task, user_id=repo_id,
                       top_k=max(limit * 6, DEFAULT_TOP_K * 4))
    return store, repo_id, raw


def search_memory(root: Path, task: str, *, limit: int = DEFAULT_TOP_K,
                  backend: Any | None = None,
                  orient: dict[str, Any] | None = None
                  ) -> list[dict[str, Any]]:
    """Mem0 = candidate retrieval; this gate = selection.  Returns at most
    ``limit`` (default 3) accepted advisory records; rejected candidates
    never reach the agent."""
    _, repo_id, raw = _fetch_candidates(root, task, limit=limit,
                                        backend=backend)
    return select_memory(raw, task=task, repo_id=repo_id, orient=orient,
                         top_k=limit)['accepted']


def explain_search(root: Path, task: str, *, limit: int = DEFAULT_TOP_K,
                   backend: Any | None = None,
                   orient: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full accept/reject audit for every candidate (debugging, tests,
    STEP 12 schema); not part of the agent-facing context."""
    _, repo_id, raw = _fetch_candidates(root, task, limit=limit,
                                        backend=backend)
    return select_memory(raw, task=task, repo_id=repo_id, orient=orient,
                         top_k=limit)


def get_all_memories(root: Path, *,
                     backend: Any | None = None) -> list[dict[str, Any]]:
    store = backend if backend is not None else resolve_backend(root)
    return store.get_all(user_id=repo_identity(root))


def update_memory(root: Path, memory_id: str, *,
                  text: str | None = None,
                  metadata: dict[str, Any] | None = None,
                  backend: Any | None = None) -> None:
    store = backend if backend is not None else resolve_backend(root)
    store.update(memory_id, text=text, metadata=metadata)


def delete_memory(root: Path, memory_id: str, *,
                  backend: Any | None = None) -> None:
    store = backend if backend is not None else resolve_backend(root)
    store.delete(memory_id)


def mark_stale(root: Path, record: dict[str, Any], reason: str, *,
               backend: Any | None = None) -> dict[str, Any]:
    """STEP 6/7: conflicting repository facts are flagged, never deleted."""
    store = backend if backend is not None else resolve_backend(root)
    metadata = dict(record['metadata'])
    metadata['status'] = 'stale'
    metadata['conflict'] = True
    metadata['stale_reason'] = reason
    metadata['stale_at'] = _utc_now()
    try:
        # persistence best-effort: computed staleness is still reported in
        # the context output even if the store update fails.
        store.update(record['id'], metadata=metadata)
    except MemoryLayerError:
        raise
    except Exception:  # noqa: S110 -- deliberate best-effort swallow
        pass
    record = dict(record)
    record['metadata'] = metadata
    return record


# --------------------------------------------------------------------------
# STEP 5/6/9: validation against ORIENT + machine-readable context synthesis
# --------------------------------------------------------------------------

def _resolve_current(orient: dict[str, Any], fact_key: str) -> Any:
    node: Any = orient
    for part in fact_key.split('.'):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    if isinstance(node, dict) and 'value' in node:
        return node['value']
    return node


def group_records(orient: dict[str, Any],
                  records: list[dict[str, Any]]) -> dict[str, list]:
    """Pure grouping: repository facts conflict-checked against ORIENT,
    everything else advisory. Current facts always win."""
    grouped: dict[str, list] = {
        'repository_facts': [],
        'workflow_preferences': [],
        'historical_lessons': [],
        'decision_records': [],
        'stale_repository_facts': [],
    }
    for record in records:
        meta = record['metadata']
        category = meta.get('category')
        entry = dict(record)
        entry['status'] = meta.get('status', 'active')
        if category == 'repository_fact':
            fact_key = meta.get('fact_key')
            current = _resolve_current(orient, fact_key) if fact_key else None
            conflict = bool(fact_key) and meta.get('fact_value') != current
            if conflict or meta.get('status') == 'stale':
                entry['status'] = 'stale'
                entry['conflict'] = bool(conflict)
                if conflict:
                    entry['current_value'] = current
                    entry['stale_reason'] = (
                        f'current orient {fact_key}={current!r} overrides '
                        f'stored {meta.get("fact_value")!r}')
                grouped['stale_repository_facts'].append(entry)
            else:
                grouped['repository_facts'].append(entry)
        elif category == 'workflow_preference':
            grouped['workflow_preferences'].append(entry)
        elif category == 'historical_lesson':
            grouped['historical_lessons'].append(entry)
        elif category == 'decision_record':
            grouped['decision_records'].append(entry)
    return grouped


def validate_against_orient(root: Path, orient: dict[str, Any], *,
                            records: list[dict[str, Any]] | None = None,
                            backend: Any | None = None,
                            persist: bool = True) -> dict[str, list]:
    store = backend if backend is not None else resolve_backend(root)
    if records is None:
        records = store.get_all(user_id=repo_identity(root))
    grouped = group_records(orient, records)
    if persist:
        for entry in grouped['stale_repository_facts']:
            if entry['metadata'].get('status') != 'stale':
                mark_stale(root, entry,
                           entry.get('stale_reason', 'conflict with ORIENT'),
                           backend=store)
    return grouped


def build_context(orient: dict[str, Any],
                  records: list[dict[str, Any]]) -> dict[str, Any]:
    """STEP 5 machine-readable shape: current facts and memory NEVER share
    a namespace; conflicts surface only under memory.stale_repository_facts."""
    return {
        'schema': SCHEMA,
        'current_facts': orient,
        'memory': group_records(orient, records),
    }


def render_context(context: dict[str, Any]) -> str:
    """Agent-facing two-layer text: the current-fact layer and the advisory
    memory layer are rendered as separate sections and never merged, so the
    precedence rule (CURRENT REPOSITORY FACTS > STORED MEMORY) stays visible
    in the context itself."""
    lines = [
        'CURRENT REPOSITORY FACTS',
        '------------------------',
        json.dumps(context.get('current_facts'), ensure_ascii=False,
                   sort_keys=True, indent=2),
        '',
        'HISTORICAL MEMORY (ADVISORY)',
        '----------------------------',
    ]
    grouped = context.get('memory') or {}
    for bucket in ('repository_facts', 'workflow_preferences',
                   'historical_lessons', 'decision_records',
                   'stale_repository_facts'):
        for record in grouped.get(bucket) or []:
            lines.append(f"[{bucket} | {record.get('status', 'active')}] "
                         f"{record.get('content')}")
    for record in context.get('retrieved_for_task') or []:
        category = record.get('metadata', {}).get('category')
        lines.append(f'[task_relevant | {category}] {record.get("content")}')
    if not any(grouped.get(bucket) or []
               for bucket in grouped) and not context.get(
                   'retrieved_for_task'):
        lines.append('(no stored memories)')
    return '\n'.join(lines)


# --------------------------------------------------------------------------
# CLI (direct module usability, STEP 10); control.py delegates here.
# --------------------------------------------------------------------------

def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     indent=2))


def _load_orient(spec: str) -> dict[str, Any]:
    """ORIENT json for the conflict gate: '-' = stdin, else a file."""
    if spec == '-':
        return json.load(sys.stdin)
    return json.loads(Path(spec).read_text(encoding='utf-8'))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Asha institutional memory (Mem0 adapter)')
    parser.add_argument('--root', default='.')
    sub = parser.add_subparsers(dest='command', required=True)

    p_add = sub.add_parser('add')
    p_add.add_argument('--category', required=True, choices=CATEGORIES)
    p_add.add_argument('--content', required=True)
    p_add.add_argument('--source', default='agent')
    p_add.add_argument('--fact-key')
    p_add.add_argument('--fact-value')
    p_add.add_argument('--tree-hash')

    p_search = sub.add_parser('search')
    p_search.add_argument('--task', required=True)
    p_search.add_argument('--limit', type=int, default=DEFAULT_TOP_K,
                          help='bounded top-k exposed memories (default 3)')
    p_search.add_argument('--explain', action='store_true',
                          help='also print the per-candidate decision audit')
    p_search.add_argument('--orient-json',
                          help="current repository facts (ORIENT json, "
                               "'-' = stdin): enables the current-fact "
                               "conflict gate")

    sub.add_parser('status')

    p_context = sub.add_parser('context')
    p_context.add_argument('--task', default='')
    p_context.add_argument('--orient-json', required=True,
                           help="'-' reads ORIENT json from stdin")
    p_context.add_argument('--format', choices=('json', 'text'),
                           default='json',
                           help="text = two-layer agent view "
                                "(facts / advisory memory)")

    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        if args.command == 'add':
            record = add_memory(
                root, args.content, args.category, source=args.source,
                fact_key=args.fact_key, fact_value=args.fact_value,
                tree_hash=args.tree_hash)
            _print_json(record)
        elif args.command == 'search':
            task = args.task
            # optional ORIENT: same current-fact conflict gate as context;
            # omitting the flag keeps the historical search contract.
            orient = (_load_orient(args.orient_json)
                      if args.orient_json else None)
            if args.explain:
                selection = explain_search(root, task, limit=args.limit,
                                           orient=orient)
                _print_json({'task': task,
                             'retrieved': selection['accepted'],
                             'decisions': selection['decisions']})
            else:
                records = search_memory(root, task, limit=args.limit,
                                        orient=orient)
                _print_json({'task': task, 'retrieved': records})
        elif args.command == 'status':
            store = resolve_backend(root)
            if not store.available:
                _print_json({'available': False,
                             'reason': store.reason,
                             'total': None, 'categories': None})
            else:
                records = store.get_all(user_id=repo_identity(root))
                counts = {category: 0 for category in CATEGORIES}
                stale = 0
                for record in records:
                    meta = record['metadata']
                    category = meta.get('category')
                    if category in counts:
                        counts[category] += 1
                    if meta.get('status') == 'stale':
                        stale += 1
                _print_json({'available': True, 'reason': None,
                             'total': len(records), 'categories': counts,
                             'stale': stale})
        elif args.command == 'context':
            orient = _load_orient(args.orient_json)
            store = resolve_backend(root)
            if not store.available:
                raise MemoryLayerError(
                    f'MEMORY UNAVAILABLE (context): {store.reason}')
            # STEP 6: conflicting repository facts are ALSO persisted as
            # stale (never deleted); grouping output = the context shape.
            grouped = validate_against_orient(root, orient,
                                              backend=store, persist=True)
            context = {
                'schema': SCHEMA,
                'current_facts': orient,
                'memory': grouped,
            }
            if args.task:
                # gated selection runs against ORIENT: conflicting
                # repository facts are hard-rejected here (facts > memory)
                selection = explain_search(root, args.task,
                                           backend=store, orient=orient)
                context['retrieved_for_task'] = selection['accepted']
                context['retrieval_audit'] = selection['decisions']
            if args.format == 'text':
                print(render_context(context))
            else:
                _print_json(context)
    except MemoryLayerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f'context: invalid orient json: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:  # broken backend at runtime: fail closed,
        #                     never crash into a traceback for the agent
        print(f'MEMORY ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
