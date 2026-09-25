"""Phase 5.1 -- prototype incremental CodeGraph cache.

PROTOTYPE BOUNDARY (spec 1.2): architectural prototype and benchmarking
artifact only, consumed by ``benchmarks/benchmark_graph_cache.py``.
This module is NOT a runtime authority: ``asha.scoping``, the eligibility
engine and the scheduler do NOT import it, and zero resolver behavior is
changed by its existence.

Cache specification (spec 2.B):
- per-file SHA-256 content hash;
- invalidation metadata: python runtime version, AST parser version
  (digest of the stdlib ``ast.py`` bytes -- the grammar ships with the
  interpreter), graph schema hash (digest of the schema descriptor);
- subgraph invalidation: a dirty file re-indexes ONLY that module; all
  dependent edges are re-derived by ``build_graph`` from cached-clean +
  re-indexed-dirty ``ModuleIndex`` entries (no depth limit, no heuristic
  reuse of stale edges);
- FAIL-CLOSED FALLBACK: payload hash mismatch, missing file, corrupt
  JSON, wrong shape, or ANY deserialization exception triggers a clean
  purge of the cache directory and a full repository re-index. The
  cached data is never trusted over a fresh parse.
"""

from __future__ import annotations

import ast as _ast
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from . import codegraph
from .ast_indexer import ImportFact, ModuleIndex, SymbolFacts, index_module
from .scoping import _module_name, _repository_py_files

_CACHE_NAMES = ('meta.json', 'files.json', 'modules.json', 'graph.json')


@dataclass
class CacheLoadResult:
    """What load() returns: engine-parity sources + graph + provenance."""

    sources: dict[str, ModuleIndex]
    graph: codegraph.CodeGraph
    mode: str            # cold | warm_hit | partial | stale_purged
                         # | fallback_purged
    reindexed: tuple[str, ...]
    purged_reason: str
    duration_ms: float
    hash_ms: float       # cost of content-hashing every file
    load_ms: float       # cost of deserializing the cache payloads


def purge(cache_dir: Path) -> None:
    """Delete every cache payload (used by the fail-closed fallback)."""
    shutil.rmtree(cache_dir, ignore_errors=True)


def _schema_hash() -> str:
    """Digest of the graph data schema (field names of every payload)."""
    descriptor = {
        'ModuleIndex': [f.name for f in fields(ModuleIndex)],
        'SymbolFacts': [f.name for f in fields(SymbolFacts)],
        'ImportFact': [f.name for f in fields(ImportFact)],
        'GraphEdge': [f.name for f in fields(codegraph.GraphEdge)],
        'CodeGraph': [f.name for f in fields(codegraph.CodeGraph)],
    }
    blob = json.dumps(descriptor, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def _ast_parser_version() -> str:
    """The AST grammar ships with the interpreter: hash the ast module."""
    return hashlib.sha256(Path(_ast.__file__).read_bytes()).hexdigest()[:16]


def _meta() -> dict[str, str]:
    return {
        'python_runtime': sys.version,
        'ast_parser_version': _ast_parser_version(),
        'graph_schema_hash': _schema_hash(),
    }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for rel in sorted(_repository_py_files(root)):
        data = (root / rel).read_bytes()
        hashes[rel] = _sha256_bytes(data)
    return hashes


def _read_source(root: Path, rel: str) -> str:
    # byte-identical convention to scoping._index_repository
    return (root / rel).read_text(encoding='utf-8', errors='replace')


# --------------------------------------------------------------------------
# payload (de)serialization
# --------------------------------------------------------------------------

def _dump_symbol(fact: SymbolFacts) -> dict[str, Any]:
    data = asdict(fact)
    for key in ('reads', 'writes', 'mutations', 'annotations', 'calls',
                'attributes', 'bases', 'decorators', 'local_names'):
        data[key] = sorted(data[key])
    return data


def _load_symbol(data: dict[str, Any]) -> SymbolFacts:
    frozen = dict(data)
    for key in ('reads', 'writes', 'mutations', 'annotations', 'calls',
                'attributes', 'bases', 'decorators', 'local_names'):
        frozen[key] = frozenset(frozen[key])
    return SymbolFacts(**frozen)  # type: ignore[arg-type]


def _dump_import(fact: ImportFact) -> dict[str, Any]:
    data = asdict(fact)
    data['names'] = [list(pair) for pair in fact.names]
    return data


def _load_import(data: dict[str, Any]) -> ImportFact:
    frozen = dict(data)
    frozen['names'] = tuple(tuple(pair) for pair in frozen['names'])
    return ImportFact(**frozen)  # type: ignore[arg-type]


def _dump_index(index: ModuleIndex) -> dict[str, Any]:
    return {
        'module': index.module,
        'symbols': [_dump_symbol(f) for f in index.symbols],
        'imports': [_dump_import(f) for f in index.imports],
        'module_writes': sorted(index.module_writes),
        'unresolved': list(index.unresolved),
    }


def _load_index(data: dict[str, Any]) -> ModuleIndex:
    return ModuleIndex(
        module=data['module'],
        symbols=tuple(_load_symbol(s) for s in data['symbols']),
        imports=tuple(_load_import(i) for i in data['imports']),
        module_writes=frozenset(data['module_writes']),
        unresolved=tuple(data['unresolved']),
    )


def _dump_graph(graph: codegraph.CodeGraph) -> dict[str, Any]:
    return {
        'nodes': list(graph.nodes),
        'edges': [[e.source, e.target, e.kind] for e in graph.edges],
        'boundaries': [[node, kind] for node, kind in graph.boundaries],
    }


def _load_graph(data: dict[str, Any]) -> codegraph.CodeGraph:
    graph = codegraph.CodeGraph(
        nodes=tuple(data['nodes']),
        edges=tuple(codegraph.GraphEdge(s, t, k) for s, t, k in
                    data['edges']),
        boundaries=tuple((node, kind) for node, kind in data['boundaries']),
    )
    if not graph.nodes or not graph.edges:
        raise ValueError('graph_cache: empty graph payload')
    return graph


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(payload, sort_keys=True),
                   encoding='utf-8')
    os.replace(tmp, path)


def _save(cache_dir: Path, meta: dict[str, str], hashes: dict[str, str],
          sources: dict[str, ModuleIndex],
          graph: codegraph.CodeGraph) -> None:
    """Persist payloads atomically; digest every payload in meta.json so
    later loads can detect corruption before trusting any of it."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    modules_path = cache_dir / 'modules.json'
    graph_path = cache_dir / 'graph.json'
    files_path = cache_dir / 'files.json'
    _atomic_write(files_path, hashes)
    _atomic_write(modules_path, {m: _dump_index(i)
                                 for m, i in sorted(sources.items())})
    _atomic_write(graph_path, _dump_graph(graph))
    full_meta: dict[str, Any] = dict(meta)
    full_meta['payload_digests'] = {
        'files.json': _sha256_bytes(files_path.read_bytes()),
        'modules.json': _sha256_bytes(modules_path.read_bytes()),
        'graph.json': _sha256_bytes(graph_path.read_bytes()),
    }
    _atomic_write(cache_dir / 'meta.json', full_meta)


# --------------------------------------------------------------------------
# load: cold / warm_hit / partial / fail-closed fallback
# --------------------------------------------------------------------------

def _full_index(root: Path) -> tuple[dict[str, ModuleIndex],
                                     codegraph.CodeGraph]:
    sources: dict[str, ModuleIndex] = {}
    for rel in sorted(_repository_py_files(root)):
        source = _read_source(root, rel)
        sources[_module_name(rel)] = index_module(_module_name(rel), source)
    return sources, codegraph.build_graph(tuple(sources.values()))


def _digest_guard(cache_dir: Path) -> None:
    """Raise before loading anything if a payload bytes do not match the
    digest recorded at save time (fail-closed corruption detection)."""
    meta = json.loads((cache_dir / 'meta.json').read_text(encoding='utf-8'))
    if meta.get('python_runtime') != _meta()['python_runtime']:
        raise ValueError('graph_cache: python runtime changed')
    if meta.get('ast_parser_version') != _meta()['ast_parser_version']:
        raise ValueError('graph_cache: ast parser version changed')
    if meta.get('graph_schema_hash') != _meta()['graph_schema_hash']:
        raise ValueError('graph_cache: graph schema hash changed')
    digests = meta['payload_digests']
    for name, expected in digests.items():
        actual = _sha256_bytes((cache_dir / name).read_bytes())
        if actual != expected:
            raise ValueError(f'graph_cache: payload hash mismatch in {name}')


def load(root: Path, cache_dir: Path) -> CacheLoadResult:
    """Load indices + graph from cache, or fall back to a full re-index.

    Never trusts the cache over a fresh parse: every failure path purges
    and re-indexes the whole repository (fail-closed).
    """
    t0 = time.perf_counter()
    hashes = _file_hashes(root)
    t_hash = time.perf_counter()
    t_load = t_hash
    meta_now = _meta()
    mode = 'cold'
    reindexed: tuple[str, ...] = ()
    purged_reason = ''
    sources: dict[str, ModuleIndex] = {}
    graph: codegraph.CodeGraph | None = None
    cache_exists = all((cache_dir / n).is_file() for n in _CACHE_NAMES)
    if not cache_exists:
        if cache_dir.exists():          # partial/stale payload -> purge
            purge(cache_dir)
            purged_reason = 'incomplete cache payload set'
        sources, graph = _full_index(root)
        mode = 'stale_purged' if purged_reason else 'cold'
    else:
        try:
            _digest_guard(cache_dir)
            stored_hashes = json.loads(
                (cache_dir / 'files.json').read_text(encoding='utf-8'))
            modules_data = json.loads(
                (cache_dir / 'modules.json').read_text(encoding='utf-8'))
            graph_data = json.loads(
                (cache_dir / 'graph.json').read_text(encoding='utf-8'))
            t_load = time.perf_counter()
            if stored_hashes == hashes:
                sources = {m: _load_index(d)
                           for m, d in modules_data.items()}
                if set(sources) != {_module_name(r) for r in hashes}:
                    raise ValueError('graph_cache: module set mismatch')
                graph = _load_graph(graph_data)
                mode = 'warm_hit'
            else:
                dirty = sorted(r for r, digest in hashes.items()
                               if stored_hashes.get(r) != digest)
                removed = sorted(set(stored_hashes) - set(hashes))
                sources = {m: _load_index(d)
                           for m, d in modules_data.items()}
                for module in (_module_name(r) for r in removed):
                    sources.pop(module, None)
                for rel in dirty:
                    source = _read_source(root, rel)
                    sources[_module_name(rel)] = index_module(
                        _module_name(rel), source)
                graph = codegraph.build_graph(tuple(sources.values()))
                mode = 'partial'
                reindexed = tuple(dirty + removed)
        except Exception as exc:
            # fail-closed: ANY cache-side failure -> purge + full re-index
            purge(cache_dir)
            purged_reason = f'{type(exc).__name__}: {exc}'
            sources, graph = _full_index(root)
            mode = 'fallback_purged'
            t_load = time.perf_counter()  # fallback cost is load cost
    if graph is None:                                # defensive (untyped)
        sources, graph = _full_index(root)
        mode = 'cold'
    if mode != 'warm_hit':    # hit: payloads already on disk, keep IO
        try:
            _save(cache_dir, meta_now, hashes, sources, graph)
        except Exception as exc:
            # a cache we cannot WRITE is not an authority; keep result
            if not purged_reason:
                purged_reason = (f'save_failed: '
                                 f'{type(exc).__name__}: {exc}')
    t_end = time.perf_counter()
    return CacheLoadResult(
        sources=sources,
        graph=graph,
        mode=mode,
        reindexed=reindexed,
        purged_reason=purged_reason,
        duration_ms=(t_end - t0) * 1000.0,
        hash_ms=(t_hash - t0) * 1000.0,
        load_ms=(t_load - t_hash) * 1000.0,
    )
