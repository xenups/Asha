"""Phase 5.1 Step 3 -- incremental graph cache integration tests
(spec 2.A deliverable: cold/hit/dirty/invalidation/fail-closed/equivalence).

Every case runs on a throwaway mini-repository under tmp_path; the real
working tree is never touched. Governance decisions are compared
cold-vs-warm through the REAL frozen engine (scoping.assess_scoping_
eligibility) -- the cache must never change a verdict, and a corrupt
cache must never produce SCOPED.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from asha import graph_cache, scoping
from asha.ast_indexer import clear_session_cache, index_module, prime_session_cache

A_SRC = 'from .b_mod import helper\n\n\ndef run():\n    return helper()\n'
B_SRC = ('def helper():\n    return 1\n\n\ndef helper2():\n    return 2\n')
UNRELATED = 'def solo():\n    return 2\n'
INIT = '"""pkg"""\n'
MAIN = 'import pkg.a\n'


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'pkg').mkdir(parents=True)
    (root / 'pkg' / '__init__.py').write_text(INIT, encoding='utf-8')
    (root / 'pkg' / 'a.py').write_text(A_SRC, encoding='utf-8')
    (root / 'pkg' / 'b_mod.py').write_text(B_SRC, encoding='utf-8')
    (root / 'pkg' / 'unrelated.py').write_text(UNRELATED,
                                               encoding='utf-8')
    (root / 'main.py').write_text(MAIN, encoding='utf-8')
    return root


def _canonical(graph):
    return (tuple(sorted(graph.nodes)),
            tuple(sorted((e.source, e.target, e.kind)
                         for e in graph.edges)),
            tuple(sorted(graph.boundaries)))


def _fresh_cold(root: Path, tmp_path: Path, tag: str):
    """Independent cold build in a virgin cache dir (ground truth)."""
    result = graph_cache.load(root, tmp_path / f'cold-{tag}')
    assert result.mode == 'cold', result.mode
    return result


def test_cold_build_populates_cache(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    result = graph_cache.load(root, cache_dir)
    assert result.mode == 'cold'
    assert result.purged_reason == ''
    assert (cache_dir / 'meta.json').is_file()
    assert (cache_dir / 'files.json').is_file()
    assert (cache_dir / 'modules.json').is_file()
    assert (cache_dir / 'graph.json').is_file()
    assert result.sources and result.graph.nodes


def test_pure_cache_hit_reuses_everything(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    first = graph_cache.load(root, cache_dir)
    second = graph_cache.load(root, cache_dir)
    assert second.mode == 'warm_hit'
    assert second.reindexed == ()
    assert second.sources == first.sources
    assert _canonical(second.graph) == _canonical(first.graph)


def test_single_file_dirty_reindexes_only_that_file(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    target = root / 'pkg' / 'unrelated.py'
    target.write_text(UNRELATED + '\n# touched\n', encoding='utf-8')
    result = graph_cache.load(root, cache_dir)
    assert result.mode == 'partial'
    assert result.reindexed == ('pkg/unrelated.py',)
    cold = _fresh_cold(root, tmp_path, 'dirty')
    assert _canonical(result.graph) == _canonical(cold.graph)
    assert result.sources == cold.sources


def test_import_addition_invalidates_and_edges_update(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    (root / 'pkg' / 'a.py').write_text(
        A_SRC + 'from . import unrelated\n', encoding='utf-8')
    before = graph_cache.load(root, tmp_path / 'pre-add')
    pre_targets = {e.target for e in before.graph.edges
                   if e.source == 'mod:pkg.a' and e.kind == 'import'}
    (root / 'pkg' / 'a.py').write_text(
        A_SRC + 'from .unrelated import solo\n', encoding='utf-8')
    result = graph_cache.load(root, cache_dir)
    assert result.mode == 'partial'
    assert result.reindexed == ('pkg/a.py',)
    targets = {e.target for e in result.graph.edges
               if e.source == 'mod:pkg.a' and e.kind == 'import'}
    assert targets - pre_targets == {'sym:pkg.unrelated:solo'}
    cold = _fresh_cold(root, tmp_path, 'add')
    assert _canonical(result.graph) == _canonical(cold.graph)


def test_import_removal_invalidates_and_edges_update(
        tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    (root / 'pkg' / 'a.py').write_text(
        A_SRC + 'from .unrelated import solo\n', encoding='utf-8')
    graph_cache.load(root, cache_dir)
    (root / 'pkg' / 'a.py').write_text(A_SRC, encoding='utf-8')
    result = graph_cache.load(root, cache_dir)
    assert result.mode == 'partial'
    assert result.reindexed == ('pkg/a.py',)
    targets = {e.target for e in result.graph.edges
               if e.source == 'mod:pkg.a' and e.kind == 'import'}
    assert 'sym:pkg.unrelated:solo' not in targets
    cold = _fresh_cold(root, tmp_path, 'remove')
    assert _canonical(result.graph) == _canonical(cold.graph)


def test_dependency_edge_change_updates_target(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    (root / 'pkg' / 'a.py').write_text(
        'from .b_mod import helper2\n\n\ndef run():\n'
        '    return helper2()\n', encoding='utf-8')
    result = graph_cache.load(root, cache_dir)
    assert result.mode == 'partial'
    assert result.reindexed == ('pkg/a.py',)
    targets = {e.target for e in result.graph.edges
               if e.source == 'mod:pkg.a' and e.kind == 'import'}
    assert 'sym:pkg.b_mod:helper2' in targets
    assert 'sym:pkg.b_mod:helper' not in targets
    cold = _fresh_cold(root, tmp_path, 'edge')
    assert _canonical(result.graph) == _canonical(cold.graph)


def test_unrelated_modules_preserved_byte_identical(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    (root / 'pkg' / 'a.py').write_text(
        A_SRC + 'from .unrelated import solo\n', encoding='utf-8')
    result = graph_cache.load(root, cache_dir)
    # untouched modules come from cache and must equal a fresh parse
    for module, rel in (('pkg.b_mod', 'pkg/b_mod.py'),
                        ('pkg.unrelated', 'pkg/unrelated.py'),
                        ('main', 'main.py')):
        fresh = index_module(module, (root / rel).read_text(
            encoding='utf-8'))
        assert result.sources[module] == fresh


def _tamper(path: Path, mutate) -> None:
    """Rewrite a cache payload with `mutate` applied to its text."""
    text = path.read_text(encoding='utf-8')
    path.write_text(mutate(text), encoding='utf-8')


def _rehash_meta(cache_dir: Path) -> None:
    """Recompute payload digests so corruption passes the digest guard
    (isolates the deserialization/shape failure paths)."""
    import hashlib
    meta_path = cache_dir / 'meta.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    meta['payload_digests'] = {
        name: hashlib.sha256((cache_dir / name).read_bytes()).hexdigest()
        for name in ('files.json', 'modules.json', 'graph.json')
    }
    meta_path.write_text(json.dumps(meta), encoding='utf-8')


def _assert_fallback(result, root: Path, tmp_path: Path, tag: str) -> None:
    assert result.mode == 'fallback_purged', result.mode
    assert result.purged_reason, 'purge reason must be recorded'
    cold = _fresh_cold(root, tmp_path, tag)
    assert result.sources == cold.sources
    assert _canonical(result.graph) == _canonical(cold.graph)


def test_truncated_cache_purges_and_rebuilds(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    payload = cache_dir / 'graph.json'
    data = payload.read_bytes()
    payload.write_bytes(data[:len(data) // 2])
    result = graph_cache.load(root, cache_dir)
    _assert_fallback(result, root, tmp_path, 'trunc')


def test_corrupt_cache_purges_and_rebuilds(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    payload = cache_dir / 'modules.json'
    data = bytearray(payload.read_bytes())
    data[len(data) // 2] ^= 0x20        # flip one byte in the middle
    payload.write_bytes(bytes(data))
    result = graph_cache.load(root, cache_dir)
    _assert_fallback(result, root, tmp_path, 'corrupt')


def test_invalid_serialization_purges_and_rebuilds(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    _tamper(cache_dir / 'graph.json', lambda _t: '{not valid json')
    _rehash_meta(cache_dir)             # digests valid, body invalid
    result = graph_cache.load(root, cache_dir)
    _assert_fallback(result, root, tmp_path, 'badjson')


def test_schema_mismatch_purges_and_rebuilds(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    meta_path = cache_dir / 'meta.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    meta['graph_schema_hash'] = '0' * 64
    meta_path.write_text(json.dumps(meta), encoding='utf-8')
    result = graph_cache.load(root, cache_dir)
    _assert_fallback(result, root, tmp_path, 'schema')


def test_hash_mismatch_purges_and_rebuilds(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    meta_path = cache_dir / 'meta.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    meta['payload_digests']['files.json'] = 'f' * 64
    meta_path.write_text(json.dumps(meta), encoding='utf-8')
    result = graph_cache.load(root, cache_dir)
    _assert_fallback(result, root, tmp_path, 'hash')


def test_incomplete_payload_set_purges(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    (cache_dir / 'modules.json').unlink()
    result = graph_cache.load(root, cache_dir)
    assert result.mode == 'stale_purged'
    assert result.purged_reason
    cold = _fresh_cold(root, tmp_path, 'incomplete')
    assert result.sources == cold.sources
    assert _canonical(result.graph) == _canonical(cold.graph)


def test_cold_warm_graph_equivalence(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    warm = graph_cache.load(root, tmp_path / 'cache')
    assert warm.mode == 'cold'          # first fill
    warm2 = graph_cache.load(root, tmp_path / 'cache')
    assert warm2.mode == 'warm_hit'
    cold = _fresh_cold(root, tmp_path, 'equiv')
    assert _canonical(warm2.graph) == _canonical(cold.graph)
    assert warm2.sources == cold.sources


def _assess(root: Path):
    return scoping.assess_scoping_eligibility(
        root, ['pkg/a.py'], 'PROVEN_DISJOINT', 'S1',
        scope_status='certain')


def test_cold_warm_scoping_decision_equivalence(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    cold_decision = _assess(root)                   # cold (no session)
    graph_cache.assemble(root, tmp_path / 'cache')  # cold fill + prime
    warm_decision = _assess(root)                   # session-seeded
    warm_hit = graph_cache.assemble(root, tmp_path / 'cache')
    assert warm_hit.mode == 'warm_hit'
    second_warm = _assess(root)
    assert warm_decision == cold_decision
    assert second_warm == cold_decision
    clear_session_cache()


def test_failed_cache_never_changes_verdict(tmp_path: Path) -> None:
    """Spec 1.C: a cache failure may only be slower, never a different
    decision -- corrupt disk, assemble (purge path), evaluate: must
    equal the cold evaluation bit for bit."""
    clear_session_cache()
    root = _repo(tmp_path)
    cold_decision = _assess(root)
    cache_dir = tmp_path / 'cache'
    graph_cache.load(root, cache_dir)
    (cache_dir / 'graph.json').write_bytes(b'{"broken":')
    failed = graph_cache.assemble(root, cache_dir)
    assert failed.mode == 'fallback_purged'
    corrupt_decision = _assess(root)
    assert corrupt_decision == cold_decision
    # and the verdict never flips toward SCOPED because of the cache
    assert corrupt_decision.eligible == cold_decision.eligible
    clear_session_cache()


def test_session_memo_content_addressed(tmp_path: Path) -> None:
    clear_session_cache()
    src = 'def f():\n    return 1\n'
    first = index_module('memo.mod', src)
    second = index_module('memo.mod', src)
    assert first is second                 # memo hit (same content)
    changed = index_module('memo.mod', src + '\n# x\n')
    assert changed is not first            # content key -> fresh parse
    other = index_module('memo.other', src)
    assert other is not first              # module key separates
    clear_session_cache()
    fresh = index_module('memo.mod', src)
    assert fresh is not first              # cleared -> re-parsed
    prime = index_module('memo.mod', src)
    prime_session_cache([('memo.mod', src, prime)])
    assert index_module('memo.mod', src) is prime
    clear_session_cache()


def test_assemble_primes_session_for_frozen_path(tmp_path: Path) -> None:
    clear_session_cache()
    root = _repo(tmp_path)
    result = graph_cache.assemble(root, tmp_path / 'cache')
    assert result.mode == 'cold'
    # the frozen path (scoping._index_repository -> index_module) now
    # reuses the disk-cache indices: same object, no re-parse
    sources, _texts, _m, _r = scoping._index_repository(root)
    for module, text in ((m, (root / rel).read_text(encoding='utf-8'))
                         for rel, m in _module_rels(root)):
        assert sources[module] is result.sources[module]
        assert index_module(module, text) is result.sources[module]
    clear_session_cache()


def _module_rels(root: Path):
    from asha.scoping import _module_name
    return [(rel, _module_name(rel))
            for rel in sorted(scoping._repository_py_files(root))]


def test_default_cache_dir_keeps_tree_clean(tmp_path: Path) -> None:
    import os
    root = _repo(tmp_path)
    default = graph_cache.default_cache_dir(root)
    assert not str(default).startswith(str(root))   # outside the tree
    os.environ['ASHA_GRAPH_CACHE_DIR'] = str(tmp_path / 'env-cache')
    try:
        assert graph_cache.default_cache_dir(root) == tmp_path / 'env-cache'
    finally:
        os.environ.pop('ASHA_GRAPH_CACHE_DIR', None)
    # a real assemble with the default dir must not create repo files
    before = {p for p in root.rglob('*')}
    graph_cache.assemble(root, None)
    after = {p for p in root.rglob('*')}
    assert before == after
    shutil.rmtree(tmp_path / 'env-cache', ignore_errors=True)
    clear_session_cache()
