"""Phase 4.2 -- three-way controlled experiment: NAIVE vs FULL
GOVERNANCE vs FAST PATH on identical inputs (§7/§11/§12/§14).

Methodology:
  * every (scenario, path, repetition) run gets a FRESH repository
    built from the same baseline -- same task, same initial tree,
    same deterministic executor, same workload;
  * every reported number is measured at run time (perf_counter_ns /
    statistics); nothing is hard-coded or asserted for greenness;
  * safety is checked against HAND-WRITTEN ground truth:
    false_fast_path counts tasks actually routed FAST_PATH whose
    ground truth is not PROVEN_DISJOINT;
  * a separate stale-observation demonstration reproduces the naive
    baseline's lost-update failure mode under a synchronized
    read-modify-write workload (§4).

Run: PYTHONPATH=<repo> python benchmarks/run_fastpath_bench.py
"""
from __future__ import annotations

import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from asha.ast_indexer import index_module
from asha.codegraph import build_graph, closure, sym_node
from asha.context_slicer import slice_context
from asha.scheduler import GovernedScheduler, default_execute

PY = sys.executable
REPS = 3
BASELINE = {
    '.gitignore': ('.jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n'
                   '.mypy_cache/\n.ruff_cache/\n'),
    'README.md': '# fixture\n',
    'pyproject.toml': '[tool.ruff]\nline-length = 88\n',
    'tests/test_ok.py': 'def test_ok():\n    assert True\n',
    'pkg/__init__.py': '',
    'pkg/module_a.py': 'VALUE_A = "original a"\n',
    'pkg/module_b.py': 'VALUE_B = "original b"\n',
    'shared.py': 'SHARED = "original shared"\n',
}

# scenario -> (workers, ground truth {wid: expected classification})
SCENARIOS: dict[str, tuple[list[dict[str, Any]], dict[str, str]]] = {}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _make_repo(root: Path) -> Path:
    repo = root / 'repo'
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 'fixture@example.com')
    _git(repo, 'config', 'user.name', 'Fixture')
    _git(repo, 'config', 'commit.gpgsign', 'false')
    for rel, text in BASELINE.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'baseline')
    return repo


def _worker(wid: str, rel: str, marker: str, *,
            reads: list[str] | None = None,
            writes: list[str] | None = None) -> dict[str, Any]:
    code = (f'from pathlib import Path; '
            f'Path({rel!r}).write_text({marker!r})')
    return {
        'id': wid, 'deps': [],
        'declared_scope': [rel],
        'reads': reads if reads is not None else [rel],
        'writes': writes if writes is not None else [rel],
        'cmd': [PY, '-c', code],
    }


def _scenarios() -> dict[str, tuple[list[dict[str, Any]],
                                    dict[str, str]]]:
    return {
        'A_disjoint': (
            [_worker('wa', 'pkg/module_a.py', '# a done\n'),
             _worker('wb', 'pkg/module_b.py', '# b done\n')],
            {'wa': 'PROVEN_DISJOINT', 'wb': 'PROVEN_DISJOINT'}),
        'B_shared_write': (
            [_worker('wb1', 'shared.py', '# writer one\n'),
             _worker('wb2', 'shared.py', '# writer two\n')],
            {'wb1': 'PROVEN_SHARED', 'wb2': 'PROVEN_SHARED'}),
        'C_read_write': (
            [_worker('wc1', 'pkg/module_a.py', '# c1\n',
                     reads=['pkg/module_b.py'],
                     writes=['pkg/module_a.py']),
             _worker('wc2', 'pkg/module_b.py', '# c2\n',
                     reads=['pkg/module_a.py'],
                     writes=['pkg/module_b.py'])],
            {'wc1': 'PROVEN_SHARED', 'wc2': 'PROVEN_SHARED'}),
        'D_single_unknown': (
            [_worker('wd1', 'pkg/module_a.py', '# lone\n')],
            {'wd1': 'UNKNOWN'}),
    }


# ---------------------------------------------------------------------
# path runners (each returns a comparable artifact + wall clock ns)
# ---------------------------------------------------------------------
def _run_naive(workers: list[dict[str, Any]], root: Path
               ) -> tuple[dict[str, Any], int]:
    repo = _make_repo(root)
    started = perf_counter_ns()
    outcomes: dict[str, int] = {}
    lock = threading.Lock()

    def execute(worker: dict[str, Any]) -> None:
        proc = subprocess.run(worker['cmd'], cwd=repo,
                              capture_output=True, timeout=60)
        with lock:
            outcomes[worker['id']] = proc.returncode

    threads = [threading.Thread(target=execute, args=(worker,))
               for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = perf_counter_ns() - started
    outputs = {worker['id']: (
        repo / worker['declared_scope'][0]).read_text(encoding='utf-8')
        for worker in workers}
    return {'path': 'naive', 'states': outcomes, 'outputs': outputs,
            'worktrees': {}, 'evidence': {}}, elapsed


def _run_scheduler(workers: list[dict[str, Any]], root: Path, *,
                   fast: bool,
                   counters: dict[str, list[int]]
                   ) -> tuple[dict[str, Any], int]:
    repo = _make_repo(root)
    sched = GovernedScheduler(repo, workers, task_id='bench42',
                              keep_worktrees=True,
                              fast_path_enabled=fast)
    # benchmark-level instrumentation (production code untouched)
    original_create = sched.dispatcher.create
    original_collect = sched._collect

    def timed_create(wid: str) -> Path:
        start = perf_counter_ns()
        path = original_create(wid)
        counters['worktree'].append(perf_counter_ns() - start)
        return path

    def timed_collect(worker: dict[str, Any], path: Path, rc: int,
                      tail: str, **kwargs: Any) -> dict[str, Any]:
        start = perf_counter_ns()
        result = original_collect(worker, path, rc, tail, **kwargs)
        counters['collect'].append(perf_counter_ns() - start)
        return result

    sched.dispatcher.create = timed_create  # type: ignore[assignment]
    sched._collect = timed_collect  # type: ignore[method-assign]

    def timed_execute(worker: dict[str, Any], path: Path) -> Any:
        start = perf_counter_ns()
        result = default_execute(worker, path)
        counters['execution'].append(perf_counter_ns() - start)
        return result

    sched.execute = timed_execute  # type: ignore[assignment]
    started = perf_counter_ns()
    report = sched.run()
    elapsed = perf_counter_ns() - started
    # final relevant state per worker: a worktree entry means that
    # worker ran FULL governance (results live in its kept worktree);
    # no entry means it ran FAST against the repo -- mixed runs must
    # be read per-worker, not per-path
    by_id = {worker['id']: worker for worker in workers}
    outputs: dict[str, str] = {}
    for wid in report['states']:
        rel = by_id[wid]['declared_scope'][0]
        if wid in report['worktrees']:
            worktree = Path(report['worktrees'][wid])
            outputs[wid] = (worktree / rel).read_text(encoding='utf-8')
        else:
            outputs[wid] = (repo / rel).read_text(encoding='utf-8')
    if fast:
        for record in report.get('routing', {}).values():
            counters['classify'].append(record['t_classify_ns'])
            counters['route'].append(record['t_route_ns'])
    artifact = {
        'path': 'fast_path' if fast else 'full_governance',
        'status': report['status'],
        'states': {wid: state['state']
                   for wid, state in report['states'].items()},
        'outputs': outputs,
        'worktrees': dict(report['worktrees']),
        'evidence': dict(report['evidence']),
        'routing': dict(report.get('routing', {})),
        'deferrals': len(report['deferral_events']),
        'report': report,
    }
    return artifact, elapsed


def _stats(samples_ns: list[int]) -> dict[str, float]:
    ordered = sorted(samples_ns)
    rank = max(0, round(0.95 * len(ordered)) - 1)
    return {
        'median_ms': statistics.median(ordered) / 1e6,
        'p95_ms': ordered[rank] / 1e6,
        'min_ms': ordered[0] / 1e6,
        'max_ms': ordered[-1] / 1e6,
    }


def _context_preflight() -> dict[str, float]:
    """Phase 4.1 index/graph/slice cost over the baseline modules
    (measured, NOT wired into the runtime routing yet)."""
    root = Path(tempfile.mkdtemp(prefix='asha42-ctx-'))
    try:
        repo = _make_repo(root)
        sources = {rel: (repo / rel).read_text(encoding='utf-8')
                   for rel in ('pkg/module_a.py', 'pkg/module_b.py',
                               'shared.py')}
        started = perf_counter_ns()
        indices = tuple(
            index_module(f'pkg.{rel.replace("/", ".").removesuffix(".py")}',
                         text)
            for rel, text in sources.items())
        index_ns = perf_counter_ns() - started
        started = perf_counter_ns()
        graph = build_graph(indices)
        graph_ns = perf_counter_ns() - started
        target = indices[0].symbol('VALUE_A')
        assert target is not None
        started = perf_counter_ns()
        root_node = sym_node(indices[0].module, 'VALUE_A')
        closure(graph, (root_node,), indices[0].module)
        slice_context(target.source, target_name='VALUE_A',
                      target_module=indices[0].module, graph=graph,
                      indices=indices)
        slice_ns = perf_counter_ns() - started
        return {'index_ms': index_ns / 1e6, 'graph_ms': graph_ns / 1e6,
                'slice_ms': slice_ns / 1e6}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _stale_demo() -> dict[str, Any]:
    """§4: the naive baseline reproduces the stale-observation failure
    mode. Two threads READ the same file before either writes
    (synchronized), then write back what they read + their marker:
    one update is provably lost."""
    root = Path(tempfile.mkdtemp(prefix='asha42-stale-'))
    try:
        repo = _make_repo(root)
        barrier = threading.Barrier(2)
        results: dict[str, str] = {}
        lock = threading.Lock()

        def rmw(label: str) -> None:
            observed = (repo / 'shared.py').read_text(encoding='utf-8')
            barrier.wait(timeout=10)      # both hold the SAME stale read
            (repo / 'shared.py').write_text(
                observed + f'# {label}\n', encoding='utf-8')
            with lock:
                results[label] = observed

        threads = [threading.Thread(target=rmw, args=('A',)),
                   threading.Thread(target=rmw, args=('B',))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        final = (repo / 'shared.py').read_text(encoding='utf-8')
        stale_reads = sum(1 for value in results.values()
                          if 'original shared' in value)
        lost = not ('# A\n' in final and '# B\n' in final)
        return {'stale_reads': stale_reads,
                'lost_update': lost, 'final_holds_both': not lost}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    scenarios = _scenarios()
    print('== three-way wall clock (identical inputs per path) ==')
    summary_rows: list[tuple[str, str, dict[str, float], int]] = []
    false_fast_path = 0
    fast_total_samples: list[int] = []
    correctness_regressions = 0
    routing_counts = {'fast': 0, 'full': 0, 'unknown': 0, 'total': 0}
    all_counters: dict[str, list[int]] = {
        'worktree': [], 'execution': [], 'collect': [],
        'classify': [], 'route': []}

    for name, (workers, ground) in scenarios.items():
        per_path: dict[str, list[int]] = {
            'naive': [], 'full_governance': [], 'fast_path': []}
        counters: dict[str, list[int]] = {
            'worktree': [], 'execution': [], 'collect': [],
            'classify': [], 'route': []}
        artifacts: dict[str, dict[str, Any]] = {}
        for rep in range(REPS):
            for path in ('naive', 'full_governance', 'fast_path'):
                root = Path(tempfile.mkdtemp(prefix='asha42-'))
                try:
                    if path == 'naive':
                        artifact, elapsed = _run_naive(workers, root)
                    else:
                        artifact, elapsed = _run_scheduler(
                            workers, root,
                            fast=(path == 'fast_path'),
                            counters=counters)
                finally:
                    shutil.rmtree(root, ignore_errors=True)
                per_path[path].append(elapsed)
                artifacts[path] = artifact
        # safety vs hand-written ground truth
        fast_artifact = artifacts['fast_path']
        assert 'routing' in fast_artifact
        for wid, record in fast_artifact['routing'].items():
            routing_counts['total'] += 1
            if record['mode'] == 'fast_path':
                routing_counts['fast'] += 1
                if ground[wid] != 'PROVEN_DISJOINT':
                    false_fast_path += 1
            else:
                routing_counts['full'] += 1
            if record['classification'] == 'UNKNOWN':
                routing_counts['unknown'] += 1
            if record['classification'] != ground[wid]:
                print(f'[classification mismatch] {name}/{wid}: '
                      f'actual={record["classification"]} '
                      f'expected={ground[wid]}')
        # fast vs full equivalence on the final run's artifacts
        full_artifact = artifacts['full_governance']
        fast_vs_full = (
            fast_artifact['states'] == full_artifact['states']
            and fast_artifact['outputs'] == full_artifact['outputs'])
        if not fast_vs_full:
            correctness_regressions += 1
            print(f'[equivalence] MISMATCH in {name}: '
                  f'states/files differ')
        fast_total_samples.extend(per_path['fast_path'])
        for path, samples in per_path.items():
            summary_rows.append((name, path, _stats(samples),
                                 len(samples)))
        for key, values in counters.items():
            all_counters[key].extend(values)

    print(f'{"scenario":<18}{"path":<18}{"median":>10}{"p95":>10}'
          f'{"min":>10}{"max":>10}{"n":>4}')
    for name, path, stats, count in summary_rows:
        print(f'{name:<18}{path:<18}{stats["median_ms"]:>10.2f}'
              f'{stats["p95_ms"]:>10.2f}{stats["min_ms"]:>10.2f}'
              f'{stats["max_ms"]:>10.2f}{count:>4}')

    context = _context_preflight()
    print(f'== context preflight (Phase 4.1, measured, not wired) ==\n'
          f'index={context["index_ms"]:.3f}ms '
          f'graph={context["graph_ms"]:.3f}ms '
          f'slice={context["slice_ms"]:.3f}ms')

    stale = _stale_demo()
    print(f'== naive stale-observation baseline (§4) ==\n'
          f'stale_reads={stale["stale_reads"]} '
          f'lost_update={stale["lost_update"]}')

    print('== component breakdown (directly measured, ms) ==')
    for key in ('classify', 'route', 'worktree', 'execution',
                'collect'):
        values = all_counters[key]
        if values:
            stats = _stats(values)
            print(f'{key:<12}n={len(values):<4} '
                  f'median={stats["median_ms"]:.3f} '
                  f'p95={stats["p95_ms"]:.3f}')
        else:
            print(f'{key:<12}not_applicable (path absent)')
    print(f'{"context":<12}index/graph/slice preflight measured '
          f'above; NOT wired into scheduler routing yet')
    print(f'{"replay":<12}not_applicable (standalone Phase 3.2 '
          f'verifier, not invoked by scheduler paths)')
    print(f'{"commitment":<12}not_applicable (standalone Phase 3.3 '
          f'verifier, not invoked by scheduler paths)')

    routing_rate = (routing_counts['fast'] / routing_counts['total']
                    if routing_counts['total'] else 0.0)
    print('== safety ==\n'
          f'false_fast_path={false_fast_path}\n'
          f'correctness_regressions={correctness_regressions}\n'
          f'unknown_never_fast=checked_in_unit_corpus')
    print('== routing ==\n'
          f'total_tasks={routing_counts["total"]}\n'
          f'fast_path_tasks={routing_counts["fast"]}\n'
          f'full_governance_tasks={routing_counts["full"]}\n'
          f'unknown_tasks={routing_counts["unknown"]}\n'
          f'fast_path_rate={routing_rate:.3f}')

    fast_stats = _stats(fast_total_samples)
    print(f'== fast path latency (scheduler total, {len(fast_total_samples)} runs) ==\n'
          f'median={fast_stats["median_ms"]:.2f}ms '
          f'p95={fast_stats["p95_ms"]:.2f}ms '
          f'min={fast_stats["min_ms"]:.2f}ms '
          f'max={fast_stats["max_ms"]:.2f}ms')
    target_pass = fast_stats['median_ms'] < 50.0
    print(f'== experimental target ==\n'
          f'FAST_PATH total < 50ms: {"PASS" if target_pass else "FAIL"} '
          f'(median {fast_stats["median_ms"]:.2f}ms)')
    print('NOTE: report-only -- no threshold was tuned to force a pass.')


if __name__ == '__main__':
    main()
