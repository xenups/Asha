"""Phase 3.1 evidence-acquisition benchmark.

Measures ONE worker evidence cycle (execute + GovernedScheduler._collect)
per repetition and decomposes it:

  T_state        git state ops: status / add / commit / rev-parse (incl.
                 the evidence re-bind)
  T_scope        git scope ops: diff --name-only / ls-files / show
                 (scope_resolver)
  T_facts        git payload diff (base..target, full content)
  T_validation   python checks: ruff / mypy / pytest  (reported SEPARATELY)
  T_canonicalize in-process residual of _collect (payload build, seal,
                 digest, atomic json write, in-memory ledger derivation)
  T_engine       = T_collect - T_validation
                 = T_state + T_scope + T_facts + T_canonicalize
  T_workload     worker execute span (the EXPLICIT GOR denominator)

GOR = T_engine / T_workload (median of per-repetition ratios).

The <175ms figure is a performance TARGET measured here, never asserted
by unit tests. Setup (repo build, worktree create) is outside all timers.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Self

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
REPO_NAME = 'bench-repo'

GITIGNORE = '.jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n'
README = '# evidence bench\n'
BASE_CORE = {
    '.gitignore': GITIGNORE,
    'README.md': README,
    'pyproject.toml': '[tool.ruff]\nline-length = 88\n',
    'tests/test_ok.py': 'def test_ok():\n    assert True\n',
    'pkg/__init__.py': '',
}

AGENT_ITERS = 200_000        # ~S-scale workload (pbkdf2, linear)
WORKER_CONTENT = 'RESULT = 1\n'


def make_repo(path: Path) -> Path:
    repo = path / REPO_NAME
    repo.mkdir(parents=True)
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 'fixture@example.com')
    _git(repo, 'config', 'user.name', 'Fixture')
    _git(repo, 'config', 'commit.gpgsign', 'false')
    for rel, text in BASE_CORE.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'baseline')
    return repo


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, check=True)
    return proc.stdout.strip()


def worker_payload() -> list[str]:
    code = (
        'import hashlib,pathlib;'
        f'hashlib.pbkdf2_hmac("sha256",b"asha-bench",b"salt",{AGENT_ITERS});'
        "pathlib.Path('pkg/out.py').parent.mkdir(parents=True,exist_ok=True);"
        f"pathlib.Path('pkg/out.py').write_text({WORKER_CONTENT!r},"
        "encoding='utf-8')"
    )
    return [PY, '-c', code]


def _classify(argv: tuple) -> str:
    if not argv:
        return 'other'
    if argv[0] == 'git':
        flat = list(argv[1:])
        if flat[:2] == ['diff', '--no-renames'] or (
                'diff' in flat and '--name-only' in flat):
            return 'scope'
        if flat and flat[0] in ('ls-files', 'show'):
            return 'scope'
        if 'diff' in flat:
            return 'facts'
        return 'state'
    joined = ' '.join(argv)
    if 'ruff' in joined or 'mypy' in joined or 'pytest' in joined:
        return 'validation'
    if 'pbkdf2_hmac' in joined:
        return 'agent'
    return 'other'


class Spy:
    """Global subprocess spy: records (phase, bucket, duration)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, float]] = []
        self.phase = 'setup'
        self._real_run = subprocess.run
        self._real_popen = subprocess.Popen
        self._in_run = False

    def __enter__(self) -> Self:
        real_run = self._real_run

        def run(*args, **kwargs):
            argv = args[0] if args else kwargs.get('args')
            # subprocess.run internally calls Popen: record ONLY at the
            # run level so one git command is not counted twice.
            self._in_run = True
            t0 = time.perf_counter()
            try:
                return real_run(*args, **kwargs)
            finally:
                self._in_run = False
                flat = tuple(str(x) for x in argv) if argv else ()
                self.events.append((
                    self.phase, _classify(flat),
                    (time.perf_counter() - t0) * 1000.0))

        def popen(*args, **kwargs):
            if self._in_run:
                return self._real_popen(*args, **kwargs)
            argv = args[0] if args else kwargs.get('args')
            t0 = time.perf_counter()
            proc = self._real_popen(*args, **kwargs)
            flat = tuple(str(x) for x in argv) if argv else ()
            self.events.append((
                self.phase, _classify(flat),
                (time.perf_counter() - t0) * 1000.0))
            return proc

        subprocess.run = run          # type: ignore[assignment]
        subprocess.Popen = popen      # type: ignore[misc, assignment]
        return self

    def __exit__(self, *exc) -> None:
        subprocess.run = self._real_run
        subprocess.Popen = self._real_popen  # type: ignore[misc]


def one_rep(repo: Path, rep: int) -> dict:
    from asha.scheduler import GovernedScheduler

    worker = {
        'id': 'worker_a',
        'declared_scope': ['pkg/out.py'],
        'reads': [],
        'writes': ['pkg/out.py'],
        'deps': [],
        'cmd': worker_payload(),
        'verify': 'pytest tests/ -q',
    }
    spy = Spy()
    with spy:
        sched = GovernedScheduler(repo, [worker],
                                  task_id=f'evidence-rep-{rep}')
        path = sched.dispatcher.create('worker_a')
        spy.phase = 'execute'
        t0 = time.perf_counter()
        real_execute = sched.execute

        def timed_execute(w, p):
            start = time.perf_counter()
            result = real_execute(w, p)
            timings['workload'] = (time.perf_counter() - start) * 1000.0
            return result

        timings: dict[str, float] = {}
        sched.execute = timed_execute  # type: ignore[assignment]
        spy.phase = 'collect'
        real_collect = sched._collect

        def timed_collect(w, p, rc, tail):
            start = time.perf_counter()
            result = real_collect(w, p, rc, tail)
            timings['collect'] = (time.perf_counter() - start) * 1000.0
            return result

        sched._collect = timed_collect  # type: ignore[assignment]
        result = sched._run_one(worker, path)
        total = (time.perf_counter() - t0) * 1000.0
        spy.phase = 'after'
        sched.dispatcher.cleanup()

    if result['state'] != 'DONE':
        raise SystemExit(f'rep{rep}: worker not DONE: {result}')
    collect_events = [e for e in spy.events if e[0] == 'collect']
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for _, bucket, dur in collect_events:
        sums[bucket] = sums.get(bucket, 0.0) + dur
        counts[bucket] = counts.get(bucket, 0) + 1
    t_collect = timings['collect']
    t_engine = t_collect - sums.get('validation', 0.0)
    t_canonical = t_engine - sums.get('state', 0.0) - \
        sums.get('scope', 0.0) - sums.get('facts', 0.0)
    return {
        't_engine': t_engine,
        't_state': sums.get('state', 0.0),
        't_scope': sums.get('scope', 0.0),
        't_facts': sums.get('facts', 0.0),
        't_canonicalize': t_canonical,
        't_validation': sums.get('validation', 0.0),
        't_collect': t_collect,
        't_workload': timings['workload'],
        't_total': total,
        'git_calls': sum(counts.get(b, 0)
                         for b in ('state', 'scope', 'facts')),
        'python_calls': sum(counts.get(b, 0)
                            for b in ('validation', 'agent', 'other')),
        'state_calls': counts.get('state', 0),
        'scope_calls': counts.get('scope', 0),
        'facts_calls': counts.get('facts', 0),
        'validation_calls': counts.get('validation', 0),
    }


def _stats(values: list[float]) -> dict:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]
    return {
        'median': round(statistics.median(values), 3),
        'p95': round(p95, 3),
        'min': round(ordered[0], 3),
        'max': round(ordered[-1], 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reps', type=int, default=15)
    parser.add_argument('--label', default='bench')
    args = parser.parse_args()

    # unique per process: Windows may briefly lock .git objects of a
    # previous run; never rmtree a dir another process may hold.
    parent = (HERE / 'results' / 'runs'
              / f'evidence-{args.label}-{time.time_ns()}')
    repo = make_repo(parent)

    runs = [one_rep(repo, rep) for rep in range(args.reps)]
    keys = ('t_engine', 't_state', 't_scope', 't_facts', 't_canonicalize',
            't_validation', 't_collect', 't_workload', 't_total')
    summary = {key: _stats([r[key] for r in runs]) for key in keys}
    summary['gor'] = _stats([r['t_engine'] / r['t_workload'] for r in runs])
    summary['counts'] = {
        'git_calls': int(statistics.median(
            r['git_calls'] for r in runs)),
        'python_calls': int(statistics.median(
            r['python_calls'] for r in runs)),
        'state_calls': int(statistics.median(
            r['state_calls'] for r in runs)),
        'scope_calls': int(statistics.median(
            r['scope_calls'] for r in runs)),
        'facts_calls': int(statistics.median(
            r['facts_calls'] for r in runs)),
        'validation_calls': int(statistics.median(
            r['validation_calls'] for r in runs)),
    }
    summary['label'] = args.label
    summary['reps'] = args.reps

    out = HERE / 'results' / 'evidence_bench.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {}
    if out.is_file():
        payload = json.loads(out.read_text(encoding='utf-8'))
    payload[args.label] = summary
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n',
                   encoding='utf-8')

    print(f'## evidence bench [{args.label}] n={args.reps}')
    for key in keys:
        stats = summary[key]
        print(f'{key:17s} median={stats["median"]:8.3f}ms '
              f'p95={stats["p95"]:8.3f} min={stats["min"]:8.3f} '
              f'max={stats["max"]:8.3f}')
    gor = summary['gor']
    print(f'GOR (engine/workload) median={gor["median"]:.3f} '
          f'p95={gor["p95"]:.3f}')
    print('counts(median):', summary['counts'])
    target = summary['t_engine']['median']
    print(f'median T_engine = {target:.1f}ms '
          f'-> target <175ms: {"ACHIEVED" if target < 175 else "NOT met"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
