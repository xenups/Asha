"""Phase 4.1 performance + measurement harness (§19/§20/§25).

Reports ONLY -- no timing value is ever a unit-test assertion:
    * AST indexing, graph construction, closure, slicing latencies
      (median/p95) for 100/500/1000 LOC inputs
    * 1/3/5/10-hop dependency chains
    * full vs surgical size, reduction ratio (SOURCE SIZE -- no
      tokenizer claim), dependency counts
    * an independent ground-truth recall re-report

Run: PYTHONPATH=<repo> python benchmarks/run_slicer_bench.py
"""
from __future__ import annotations

import statistics
from collections.abc import Callable
from time import perf_counter_ns
from typing import TypeVar

Result = TypeVar('Result')

from asha.ast_indexer import index_module
from asha.codegraph import build_graph, closure, sym_node
from asha.context_slicer import slice_context

REPEATS = 25


def _synthetic_module(target_loc: int) -> str:
    lines = ['CONST_A = 1', 'CONST_B = 2', 'CONST_C = 3', '']
    helper = 0
    while len(lines) + 6 <= target_loc:
        lines.extend([
            f'def fn_{helper}(value, limit=CONST_A):',
            '    total = value + CONST_B',
            '    items = [part for part in (total, limit)]',
            f'    return (helper_{helper % 7}(items)',
            f'            + helper_{(helper + 1) % 7}(total)',
            f'            + helper_{(helper + 2) % 7}(limit)',
            '            + CONST_C)',
            '',
        ])
        helper += 1
    for index in range(min(7, helper)):
        lines.extend([
            f'def helper_{index}(items):',
            f'    """Helper {index}."""',
            '    collected = []',
            '    for item in items:',
            '        collected.append(item + CONST_B)',
            '    return len(collected) + CONST_A + CONST_C',
            '',
        ])
    # forward references: fn_i calls helper of NEXT module position --
    # keep single-module so all helpers are local symbols
    body = '\n'.join(lines)
    return body + '\n'


def _chain_modules(hops: int) -> tuple[tuple[str, str], ...]:
    specs: list[tuple[str, str]] = [
        ('pkg.h0', 'class Token:\n    pass\n')]
    for position in range(1, hops + 1):
        imported = 'Token' if position == 1 else f'node{position - 1}'
        specs.append((
            f'pkg.h{position}',
            (f'from pkg.h{position - 1} import {imported}\n'
             f'\n'
             f'def node{position}(token):\n'
             f'    return make(token) if token else {imported}\n'
             f'\n'
             f'def make(token):\n'
             f'    return token\n')))
    return tuple(specs)


def _pct(samples_ns: list[int]) -> tuple[float, float]:
    ordered = sorted(samples_ns)
    median = statistics.median(ordered) / 1e6
    rank = max(0, round(0.95 * len(ordered)) - 1)
    return median, ordered[rank] / 1e6


def _time(fn: Callable[[], Result]) -> tuple[int, Result]:
    start = perf_counter_ns()
    result = fn()
    return perf_counter_ns() - start, result


def bench_loc_corpus() -> None:
    print('== corpus size (LOC) ==')
    print(f'{"LOC":>6} {"index p50":>10} {"p95":>8} '
          f'{"graph p50":>10} {"p95":>8} {"closure p50":>11} '
          f'{"p95":>8} {"slice p50":>9} {"p95":>8} '
          f'{"full B":>8} {"surg B":>8} {"red%":>6} {"deps":>5}')
    for loc in (100, 500, 1000):
        source = _synthetic_module(loc)
        actual_loc = source.count('\n')
        index_times: list[int] = []
        graph_times: list[int] = []
        closure_times: list[int] = []
        slice_times: list[int] = []
        for _ in range(REPEATS):
            elapsed, module_index = _time(
                lambda: index_module('bench.mod', source))
            index_times.append(elapsed)
            elapsed, module_graph = _time(
                lambda: build_graph((module_index,)))
            graph_times.append(elapsed)
            root = sym_node('bench.mod', 'fn_0')
            elapsed, _loc_reach = _time(
                lambda: closure(module_graph, (root,), 'bench.mod'))
            closure_times.append(elapsed)
            facts = module_index.symbol('fn_0')
            assert facts is not None
            elapsed, sliced = _time(lambda: slice_context(
                facts.source, target_name='fn_0',
                target_module='bench.mod', graph=module_graph,
                indices=(module_index,)))
            slice_times.append(elapsed)
        i50, i95 = _pct(index_times)
        g50, g95 = _pct(graph_times)
        c50, c95 = _pct(closure_times)
        s50, s95 = _pct(slice_times)
        reduction = 1 - sliced.surgical_bytes / sliced.full_bytes
        print(f'{actual_loc:>6} {i50:>10.3f} {i95:>8.3f} '
              f'{g50:>10.3f} {g95:>8.3f} {c50:>11.3f} {c95:>8.3f} '
              f'{s50:>9.3f} {s95:>8.3f} '
              f'{sliced.full_bytes:>8} {sliced.surgical_bytes:>8} '
              f'{reduction * 100:>6.1f} {len(sliced.stubs):>5}')


def bench_hop_chains() -> None:
    print('== dependency chain hops ==')
    print(f'{"hops":>5} {"index p50":>10} {"p95":>8} '
          f'{"graph p50":>10} {"p95":>8} {"closure p50":>11} '
          f'{"p95":>8} {"slice p50":>9} {"p95":>8} {"deps":>5} '
          f'{"reachable":>9}')
    for hops in (1, 3, 5, 10):
        specs = _chain_modules(hops)
        index_times: list[int] = []
        graph_times: list[int] = []
        closure_times: list[int] = []
        slice_times: list[int] = []
        for _ in range(REPEATS):
            elapsed, indices = _time(lambda: tuple(
                index_module(name, source) for name, source in specs))
            index_times.append(elapsed)
            elapsed, chain_graph = _time(lambda: build_graph(indices))
            graph_times.append(elapsed)
            root = sym_node(f'pkg.h{hops}', f'node{hops}')
            target_module = f'pkg.h{hops}'
            elapsed, reach = _time(
                lambda: closure(chain_graph, (root,), target_module))
            closure_times.append(elapsed)
            facts = indices[-1].symbol(f'node{hops}')
            assert facts is not None
            elapsed, sliced = _time(lambda: slice_context(
                facts.source, target_name=f'node{hops}',
                target_module=target_module, graph=chain_graph,
                indices=indices))
            slice_times.append(elapsed)
        i50, i95 = _pct(index_times)
        g50, g95 = _pct(graph_times)
        c50, c95 = _pct(closure_times)
        s50, s95 = _pct(slice_times)
        print(f'{hops:>5} {i50:>10.3f} {i95:>8.3f} '
              f'{g50:>10.3f} {g95:>8.3f} {c50:>11.3f} {c95:>8.3f} '
              f'{s50:>9.3f} {s95:>8.3f} {len(sliced.stubs):>5} '
              f'{len(reach.reachable):>9}')


def ground_truth_recall() -> None:
    """Independent re-report (hand-written expectation, §16)."""
    specs = _chain_modules(3)
    indices = tuple(index_module(name, source)
                    for name, source in specs)
    graph = build_graph(indices)
    facts = indices[-1].symbol('node3')
    assert facts is not None
    sliced = slice_context(facts.source, target_name='node3',
                           target_module='pkg.h3', graph=graph,
                           indices=indices)
    extracted = set()
    for node in sliced.closure.reachable:
        if node.startswith('sym:'):
            extracted.add(node.split(':', 2)[2])
        elif node.startswith('ext:'):
            extracted.add(node.split(':', 1)[1].rsplit('.', 1)[-1])
        elif node.startswith('builtin:'):
            extracted.add(node.split(':', 1)[1])
    expected = {'Token', 'node1', 'node2'}
    missing = expected - extracted
    recall = len(expected & extracted) / len(expected)
    print(f'== ground truth (independent) ==\n'
          f'expected: {sorted(expected)}\n'
          f'extracted: {sorted(extracted)}\n'
          f'missing: {sorted(missing)}  recall: {recall:.3f}')


def main() -> None:
    bench_loc_corpus()
    bench_hop_chains()
    ground_truth_recall()
    print('NOTE: source-size reduction (no tokenizer in core, §20); '
          'latency figures are report-only (§21/§25).')


if __name__ == '__main__':
    main()
