"""Phase 5.1 -- prototype benchmark for the incremental graph cache
(asha.graph_cache is a PROTOTYPE, not a runtime authority).

Measures (spec 2.B):
- cold indexing (full repository from scratch);
- warm pure cache-hit (target < 50ms -- engineering target, not a
  correctness gate: correctness and deterministic invalidation win);
- warm re-index after a SINGLE-file dirty modification;
- invalidation integrity: modifying an import edge must purge that
  module and update the dependent subgraph, reusing clean modules;
- fail-closed fallback: corrupted payload AND missing payload file must
  purge + full re-index (never return poisoned cache data).

Runs entirely on a temp copy of the repository py files; the real
working tree is never touched (tree stays clean).
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from asha import graph_cache, scoping
from asha.ast_indexer import index_module

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / 'benchmarks' / 'results'


def _copy_repo(dst: Path) -> None:
    for rel in sorted(scoping._repository_py_files(ROOT)):
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / rel).read_bytes())


def main() -> int:
    results: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix='graph_cache_bench_') as tmp:
        tmp_root = Path(tmp) / 'repo'
        cache_dir = Path(tmp) / 'cache'
        _copy_repo(tmp_root)

        # ---- 1. cold: full repository from scratch (incl. cache save)
        t0 = time.perf_counter()
        cold = graph_cache.load(tmp_root, cache_dir)
        cold_ms = (time.perf_counter() - t0) * 1000.0
        assert cold.mode == 'cold', cold.mode

        # ---- 2. warm pure cache-hit (nothing dirty)
        t0 = time.perf_counter()
        hit = graph_cache.load(tmp_root, cache_dir)
        hit_ms = (time.perf_counter() - t0) * 1000.0
        assert hit.mode == 'warm_hit', hit.mode
        assert len(hit.graph.edges) == len(cold.graph.edges)
        assert dict(hit.sources) == dict(cold.sources)

        # ---- 3. single-file dirty modification (warm re-index)
        dirty_file = tmp_root / 'asha' / 'metrics.py'
        with dirty_file.open('a', encoding='utf-8') as handle:
            handle.write('\n# graph-cache bench: dirty marker\n')
        t0 = time.perf_counter()
        partial = graph_cache.load(tmp_root, cache_dir)
        dirty_ms = (time.perf_counter() - t0) * 1000.0
        assert partial.mode == 'partial', partial.mode
        assert partial.reindexed == ('asha/metrics.py',), partial.reindexed
        assert len(partial.graph.edges) >= len(hit.graph.edges) - 4

        # ---- 4. invalidation integrity: NEW import edge must appear,
        # only the edited module re-indexes, clean modules reused byte-
        # identical to a fresh parse of their file.
        edited_rel = 'tests/test_context_slicer.py'
        clean_edge = ('mod:tests.test_context_slicer',
                      'mod:asha.report_format', 'import')
        before_edges = {e.source for e in partial.graph.edges}  # sanity
        assert 'mod:tests.test_context_slicer' in before_edges
        with (tmp_root / edited_rel).open('a', encoding='utf-8') as handle:
            handle.write('\nimport asha.report_format  # graph-cache bench\n')
        t0 = time.perf_counter()
        edge_run = graph_cache.load(tmp_root, cache_dir)
        edge_ms = (time.perf_counter() - t0) * 1000.0
        edge_keys = {(e.source, e.target, e.kind)
                     for e in edge_run.graph.edges}
        reindex_ok = edge_run.reindexed == (edited_rel,)
        added_ok = clean_edge in edge_keys
        # only ONE module re-indexed -> every other module must equal a
        # fresh parse of its file (cache reuse integrity)
        reuse_ok = True
        for rel in sorted(scoping._repository_py_files(tmp_root))[:12]:
            module = scoping._module_name(rel)
            if module == scoping._module_name(edited_rel):
                continue
            fresh = index_module(
                module, (tmp_root / rel).read_text(encoding='utf-8',
                                                   errors='replace'))
            if fresh != edge_run.sources[module]:
                reuse_ok = False
                break
        integrity_ok = reindex_ok and added_ok and reuse_ok

        # ---- 5a. corrupted payload -> purge + full re-index
        (cache_dir / 'graph.json').write_bytes(b'{"broken": not-json')
        corrupt = graph_cache.load(tmp_root, cache_dir)
        fresh_sources, fresh_graph = graph_cache._full_index(tmp_root)
        corrupt_ok = (corrupt.mode == 'fallback_purged'
                      and corrupt.graph.edges == fresh_graph.edges
                      and dict(corrupt.sources) == dict(fresh_sources)
                      and corrupt.purged_reason != '')

        # ---- 5b. missing payload file -> purge + full re-index
        (cache_dir / 'files.json').unlink()
        missing = graph_cache.load(tmp_root, cache_dir)
        missing_ok = (missing.mode == 'stale_purged'
                      and missing.graph.edges == fresh_graph.edges
                      and dict(missing.sources) == dict(fresh_sources))

        results = {
            'cold_ms': round(cold_ms, 1),
            'cache_hit_ms': round(hit_ms, 1),
            'warm_single_dirty_ms': round(dirty_ms, 1),
            'import_edge_dirty_ms': round(edge_ms, 1),
            'target_50ms_hit': hit_ms < 50.0,
            'invalidation_integrity': integrity_ok,
            'fallback_corrupted_cache': corrupt_ok,
            'fallback_missing_file': missing_ok,
            'graph_nodes': len(cold.graph.nodes),
            'graph_edges': len(cold.graph.edges),
            'modules': len(cold.sources),
            'corrupt_purged_reason': corrupt.purged_reason,
        }

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / 'graph_cache_bench.json').write_text(
        json.dumps(results, indent=2, sort_keys=True), encoding='utf-8')

    print('GRAPH CACHE PROTOTYPE BENCHMARK (temp copy, real tree untouched)')
    print(f"  modules indexed:                    {results['modules']}")
    print(f"  cold indexing:                      "
          f"{results['cold_ms']} ms")
    print(f"  warm cache-hit (target < 50ms):      {results['cache_hit_ms']} "
          f"ms -> {'HIT' if results['target_50ms_hit'] else 'MISS'}")
    print(f"  warm single-file dirty re-index:     "
          f"{results['warm_single_dirty_ms']} ms")
    print(f"  import-edge dirty re-index:          "
          f"{results['import_edge_dirty_ms']} ms")
    print(f"  invalidation integrity test:         "
          f"{'PASS' if results['invalidation_integrity'] else 'FAIL'}")
    print(f"  fallback on corrupted cache:         "
          f"{'PASS' if results['fallback_corrupted_cache'] else 'FAIL'}"
          f"  (purged: {results['corrupt_purged_reason']})")
    print(f"  fallback on missing payload file:    "
          f"{'PASS' if results['fallback_missing_file'] else 'FAIL'}")
    ok = all((results['invalidation_integrity'],
              results['fallback_corrupted_cache'],
              results['fallback_missing_file']))
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
