#!/usr/bin/env python3
"""Empirical A/B/C evaluation harness for the Asha architecture.

Conditions (identical task set, identical tool binaries; the only intended
difference is the available Asha context):

    baseline      : no ORIENT, no MEM0        (agent-layer metrics n/a here)
    orient        : ORIENT available
    orient_mem0   : ORIENT + MEM0

What this harness actually measures (tool-level, deterministic, reproducible):
    * scope accuracy vs maintainer-annotated ground truth
      (match / under / over / uncertain; under-scoping = correctness failure)
    * verification cost: real check matrix executed per historical commit
      (checks, exit codes, durations) -- replayed under CURRENT tool
      binaries against each era's own config files
    * ORIENT exposure: is the ground-truth source directory present in the
      orient layout, and orient runtime (tool-level, NOT agent wall-clock)
    * MEM0: lesson retrieval, distractor exclusion, precedence
      (current ORIENT facts > stored memory)

What it deliberately does NOT measure: agent outcomes (first hypothesis,
final correctness, regressions) -- those need live multi-session agent runs.
They are reported as null + unavailable_reasons, NEVER zero-filled
(STEP 18 of the evaluation protocol).

Reproduce:
    python benchmarks/run_eval.py --condition all
No daemon, no model, no new persistence: results are plain JSON + Markdown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / '.hermes' / 'tools'
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

CONDITIONS = ('baseline', 'orient', 'orient_mem0')
RANK = {'S0': 0, 'S1': 1, 'S2': 2, 'S3': 3, 'S4': 4}
SCHEMA = 1

# Agent-layer metrics cannot be produced by a tool-level replay.
AGENT_LAYER_UNAVAILABLE = {
    'final_correctness_rate':
        'requires a live agent implementation per task and condition; '
        'no agent execution in this harness pass',
    'source_of_truth_error_rate':
        'requires observing each agent\'s FIRST substantive hypothesis; '
        'not measurable without agent runs',
    'mean_time_to_first_correct_hypothesis_ms':
        'requires live agent event streams; wall-clock model execution '
        'is not reproducible here',
    'regressed_decision_rate':
        'requires paired agent decisions across tasks over time; '
        'retrieval-layer proxies are reported instead',
}


def _git(root: Path, *args: str, input_text: str | None = None
         ) -> subprocess.CompletedProcess:
    return subprocess.run(['git', *args], cwd=root, capture_output=True,
                          text=True, timeout=120, input=input_text)


def load_tasks(path: Path) -> tuple[list[dict], str]:
    raw = path.read_text(encoding='utf-8')
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return records, hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]


def classify_scope(observed: str, observed_status: str,
                   expected: str) -> tuple[str, bool]:
    """-> (match|under|over, uncertain_flag). Under = observed rank BELOW
    ground truth (correctness failure); over = above (cost signal)."""
    uncertain = observed_status == 'uncertain'
    if RANK[observed] == RANK[expected]:
        return 'match', uncertain
    if RANK[observed] < RANK[expected]:
        return 'under', uncertain
    return 'over', uncertain


def orient_exposure(orient: dict, ground_truth_source: list[str],
                    ground_truth_tests: list[str]) -> dict[str, Any]:
    """Is the ground truth visible in orient's (directory-level) layout?
    Root-level files are NOT enumerated by orient -- reported honestly as
    not exposed, with the reason recorded."""
    roots = {entry['path'] for entry in orient['layout']['source_roots']}
    roots |= {entry['path'] for entry in orient['layout']['test_roots']}
    tests = {entry['path'] for entry in orient['layout']['test_roots']}

    def parent(path: str) -> str:
        return path.rsplit('/', 1)[0] if '/' in path else ''

    src = ground_truth_source[0] if ground_truth_source else ''
    src_parent = parent(src)
    if not src_parent:
        source_exposed: bool | None = False
        note = 'root-level file: orient reports directories, not root files'
    else:
        source_exposed = src_parent in roots
        note = None
    test_exposed: bool | None = None
    if ground_truth_tests:
        test_parent = parent(ground_truth_tests[0])
        test_exposed = test_parent in tests or test_parent in roots
    return {'source_exposed': source_exposed, 'test_exposed': test_exposed,
            'note': note}


def mem0_probe(task: dict, orient: dict, distractor_lesson: str,
               backend: Any) -> dict[str, Any]:
    """Controlled memory experiment (STEP 14): relevant lesson retrieval,
    distractor exclusion, and CURRENT FACTS > STORED MEMORY precedence."""
    import memory

    root = Path(task['_root'])
    records: list[dict] = []
    if task.get('historical_lesson'):
        records.append(memory.add_memory(
            root, task['historical_lesson'], 'historical_lesson',
            source='benchmark-seed', backend=backend))
    records.append(memory.add_memory(
        root, distractor_lesson, 'historical_lesson',
        source='benchmark-seed', backend=backend))
    conflict = task['control_conflict']
    records.append(memory.add_memory(
        root, 'stored belief before current tree', 'repository_fact',
        source='benchmark-seed', fact_key=conflict['fact_key'],
        fact_value=conflict['fact_value'], backend=backend))

    hits = memory.search_memory(root, task['task_description'], limit=5,
                                backend=backend)
    hit_ids = {hit['id'] for hit in hits}
    relevant = next((r for r in records
                     if r['content'] == task.get('historical_lesson')),
                    None)
    distractor = next(r for r in records
                      if r['content'] == distractor_lesson)

    context = memory.build_context(
        orient, memory.get_all_memories(root, backend=backend))
    stale = context['memory']['stale_repository_facts']
    conflict_entry = next(
        (e for e in stale
         if e['metadata'].get('fact_key') == conflict['fact_key']), None)
    current = (conflict_entry or {}).get('current_value')
    precedence_ok = (
        conflict_entry is not None
        and conflict_entry['status'] == 'stale'
        and conflict_entry['conflict'] is True
        and current != conflict['fact_value']
        and json.dumps(context['current_facts'], sort_keys=True)
        == json.dumps(orient, sort_keys=True))
    return {
        'lesson_available': bool(task.get('historical_lesson')),
        'lesson_retrieved': (relevant is not None
                             and relevant['id'] in hit_ids)
        if relevant else None,
        'distractor_injected': distractor['id'] in hit_ids,
        'retrieved_count': len(hits),
        'precedence_current_wins': precedence_ok,
        'current_value': current,
        'stored_value': conflict['fact_value'],
        'stale_marked': conflict_entry is not None,
    }


def aggregate(entries: list[dict], condition: str,
              task_count: int) -> dict[str, Any]:
    """Metrics for one condition. Unavailable => None + reason, never 0."""

    def mean(values: list[float]) -> float | None:
        values = [v for v in values if v is not None]
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    def rate(count: int, total: int) -> float | None:
        if total == 0:
            return None
        return round(count / total, 4)

    classes = [e['scope_class'] for e in entries]
    matches = classes.count('match')
    unders = classes.count('under')
    overs = classes.count('over')
    uncertains = 0
    for e in entries:
        if e.get('scope_uncertain'):
            uncertains += 1
    verifications: list[dict[str, Any]] = []
    for e in entries:
        v = e.get('verification')
        if v is not None:
            verifications.append(v)
    verif_times = [v['verification_time_ms'] for v in verifications]
    exposed: list[dict[str, Any]] = [
        e['orient'] for e in entries if e.get('orient') is not None]
    mem0_all = [e['mem0'] for e in entries if e.get('mem0')]
    mem0s = [m for m in mem0_all if m.get('available')]
    mem0_down = len(mem0_all) - len(mem0s)

    metrics: dict[str, Any] = {
        'final_correctness_rate': None,
        'source_of_truth_error_rate': None,
        'mean_time_to_first_correct_hypothesis_ms': None,
        'regressed_decision_rate': None,
        'mean_verification_time_ms': mean(verif_times),
        'scope_accuracy': rate(matches, task_count),
        'under_scope_rate': rate(unders, task_count),
        'over_scope_rate': rate(overs, task_count),
        'uncertain_rate': rate(uncertains, task_count),
    }
    counts: dict[str, Any] = {
        'scope_match': f'{matches} / {task_count}',
        'scope_under': f'{unders} / {task_count}',
        'scope_over': f'{overs} / {task_count}',
        'scope_uncertain': f'{uncertains} / {task_count}',
        'verification_all_passed': None,
        'orient_source_exposed': None,
        'orient_test_exposed': None,
        'mean_orient_ms': None,
        'lesson_retrieved': None,
        'distractor_excluded': None,
        'precedence_preserved': None,
        'mean_mem0_probe_ms': None,
    }
    if verifications:
        passed = 0
        for v in verifications:
            if all(c['status'] in ('passed', 'skipped')
                   for c in v['checks']):
                passed += 1
        counts['verification_all_passed'] = (
            f'{passed} / {len(verifications)}')
    if condition in ('orient', 'orient_mem0') and exposed:
        src_ok = 0
        for entry_orient in exposed:
            if entry_orient['source_exposed']:
                src_ok += 1
        counts['orient_source_exposed'] = f'{src_ok} / {len(exposed)}'
        tests: list[Any] = [e['test_exposed'] for e in exposed
                            if e['test_exposed'] is not None]
        if tests:
            tests_ok = 0
            for test_flag in tests:
                if test_flag:
                    tests_ok += 1
            counts['orient_test_exposed'] = (
                f'{tests_ok} / {len(tests)}')
        counts['mean_orient_ms'] = mean([e['orient_ms'] for e in exposed])
    if condition == 'orient_mem0' and mem0s:
        with_lesson = [m for m in mem0s if m['lesson_available']]
        retrieved = [m for m in with_lesson if m['lesson_retrieved']]
        counts['lesson_retrieved'] = (
            f'{len(retrieved)} / {len(with_lesson)}' if with_lesson
            else '0 / 0 (no lessons in task set)')
        excluded = [m for m in mem0s if not m['distractor_injected']]
        counts['distractor_excluded'] = f'{len(excluded)} / {len(mem0s)}'
        prec = [m for m in mem0s if m['precedence_current_wins']]
        counts['precedence_preserved'] = f'{len(prec)} / {len(mem0s)}'
        counts['mean_mem0_probe_ms'] = mean([m['probe_ms'] for m in mem0s])
        if mem0_down:
            counts['mem0_unavailable'] = (
                f'{mem0_down} / {len(mem0_all)} (reason in task records)')
    return {'metrics': metrics, 'counts': counts,
            'unavailable_reasons': dict(AGENT_LAYER_UNAVAILABLE)}


def render_report(base_id: str, task_sha: str, task_count: int,
                  condition_runs: dict[str, dict]) -> str:
    lines = [
        '# Asha empirical evaluation -- ' + base_id,
        '',
        '## Executive summary',
        '',
        (f'- Sample size: **{task_count} tasks** (task-set `{task_sha}`), '
         'single-repository historical sample (Asha-Harness itself).'),
        '- Conditions: ' + ', '.join(sorted(condition_runs)) + '.',
        ('- Agent-layer outcomes were **not executed** in this pass: cells '
         'are `n/a` with a recorded reason -- never zero-filled.'),
        '',
        '## Comparison table',
        '',
        '| Metric | Baseline | +Orient | +Orient+Mem0 |',
        '| --- | --- | --- | --- |',
    ]

    def cell(cond: str, key: str, kind: str = 'count') -> str:
        run = condition_runs.get(cond)
        if run is None:
            return 'not run'
        bucket = run['metrics'] if kind == 'metric' else run['counts']
        value = bucket.get(key)
        if value is None:
            return 'n/a'
        return str(value)

    rows = [
        ('Final correctness', 'final_correctness_rate', 'metric'),
        ('Source-of-truth errors', 'source_of_truth_error_rate', 'metric'),
        ('Time-to-first-correct (ms)',
         'mean_time_to_first_correct_hypothesis_ms', 'metric'),
        ('Regressed decisions', 'regressed_decision_rate', 'metric'),
        ('Verification cost mean (ms)', 'mean_verification_time_ms',
         'metric'),
        ('Scope accuracy', 'scope_accuracy', 'metric'),
        ('Under-scope rate', 'under_scope_rate', 'metric'),
        ('Over-scope rate', 'over_scope_rate', 'metric'),
        ('Scope match (count)', 'scope_match', 'count'),
        ('Verification all passed', 'verification_all_passed', 'count'),
        ('ORIENT source exposed', 'orient_source_exposed', 'count'),
        ('ORIENT test exposed', 'orient_test_exposed', 'count'),
        ('Lesson retrieved', 'lesson_retrieved', 'count'),
        ('Distractor excluded', 'distractor_excluded', 'count'),
        ('Precedence preserved', 'precedence_preserved', 'count'),
    ]
    for label, key, kind in rows:
        cells = [cell(cond, key, kind) for cond in
                 ('baseline', 'orient', 'orient_mem0')]
        lines.append(f'| {label} | {cells[0]} | {cells[1]} | {cells[2]} |')

    lines += ['', '## Interpretation', '', '### Observed', '']
    base = condition_runs.get('baseline')
    if base:
        counts = base['counts']
        lines.append(
            f"- Scope replay: {counts['scope_match']} exact matches, "
            f"{counts['scope_under']} under-scoped, "
            f"{counts['scope_over']} over-scoped, "
            f"{counts['scope_uncertain']} uncertain "
            '(condition-independent tool-level result).')
        if counts.get('verification_all_passed'):
            lines.append(
                '- Verification replay at historical commits: '
                f"{counts['verification_all_passed']} fully passed "
                '(current tool binaries, per-era configs).')
    orient_run = condition_runs.get('orient', condition_runs.get(
        'orient_mem0'))
    if orient_run and orient_run['counts'].get('orient_source_exposed'):
        lines.append(
            '- ORIENT exposure: source '
            f"{orient_run['counts']['orient_source_exposed']}, tests "
            f"{orient_run['counts']['orient_test_exposed']} "
            f"(mean orient "
            f"{orient_run['counts']['mean_orient_ms']} ms).")
    mem_run = condition_runs.get('orient_mem0')
    if mem_run:
        lines.append(
            '- MEM0 layer: lesson retrieval '
            f"{mem_run['counts']['lesson_retrieved']}, distractor "
            f"exclusion {mem_run['counts']['distractor_excluded']}, "
            'precedence preserved '
            f"{mem_run['counts']['precedence_preserved']}.")
    lines += ['', '### Interpretation (non-causal)', '',
              ('- Scope/verification numbers are identical across '
               'conditions by construction: they are deterministic replays '
               'of the same commits, not agent behavior.'),
              ('- ORIENT/MEM0 rows are tool-layer proxies (exposure, '
               'retrieval, precedence). They do NOT demonstrate that an '
               'agent performs better -- agent outcomes were not run.'),
              ('- No causal claim about Asha improving agents is '
               'supported by this pass.'), '']

    reasons = (base or next(iter(condition_runs.values()))
               )['unavailable_reasons']
    lines += ['## Limitations', '']
    for metric, reason in sorted(reasons.items()):
        lines.append(f'- `{metric}`: n/a -- {reason}.')
    lines += [
        ('- Sample size is small (single repository, historical, '
         f'n={task_count}); report counts, not significance.'),
        ('- Task-selection bias: only tasks with observable ground truth '
         '(merged commits with replayable gates) are included.'),
        ('- Model/version dependence: not applicable to tool-layer '
         'metrics; agent-layer metrics are entirely unmeasured in this '
         'pass.'),
        ('- Verification replay uses CURRENT tool binaries against each '
         "era's config, so drift between eras is measured too."),
        ('- Scope ground truth is a maintainer policy annotation '
         '(README section 4): it measures implementation conformance to '
         'the documented policy, not the validity of the policy '
         'itself.'),
        '',
    ]
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--condition', default='all',
                        choices=[*CONDITIONS, 'all'])
    parser.add_argument('--tasks', default=str(REPO / 'benchmarks'
                                               / 'tasks.jsonl'))
    parser.add_argument('--results-dir', default=str(REPO / 'benchmarks'
                                                      / 'results'))
    parser.add_argument('--repo', default=str(REPO),
                        help='repository to clone (default: this repo)')
    parser.add_argument('--allow-dirty', action='store_true')
    parser.add_argument('--skip-verification', action='store_true',
                        help='skip check-matrix replay (fast path)')
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    conditions = CONDITIONS if args.condition == 'all' else (
        args.condition,)

    # STEP 15: canonical runs require a clean tree.
    status = _git(repo, 'status', '--porcelain')
    if status.returncode != 0:
        print('EVAL ERROR: git status failed: ' + status.stderr.strip(),
              file=sys.stderr)
        return 1
    if status.stdout.strip() and not args.allow_dirty:
        print('EVAL REFUSED: working tree is dirty; commit first or pass '
              '--allow-dirty (non-canonical run).', file=sys.stderr)
        return 1

    head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    tree = _git(repo, 'rev-parse', 'HEAD^{tree}').stdout.strip()
    tasks_path = Path(args.tasks)
    records, task_sha = load_tasks(tasks_path)
    if not records:
        print('EVAL REFUSED: empty task set', file=sys.stderr)
        return 1

    date = datetime.now(timezone.utc).isoformat(timespec='seconds')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    base_id = f'{stamp}_{head[:7]}_{task_sha}'

    import check_runner
    import memory
    import project_map
    import scope_resolver

    work = Path(tempfile.mkdtemp(prefix='asha-eval-'))
    run_by_condition: dict[str, dict] = {}
    try:
        clone = work / 'repo'
        proc = _git(work, 'clone', '--quiet', str(repo), str(clone))
        if proc.returncode != 0:
            print('EVAL ERROR: clone failed: ' + proc.stderr.strip(),
                  file=sys.stderr)
            return 1
        empty = _git(clone, 'mktree', input_text='').stdout.strip()

        for condition in conditions:
            entries: list[dict] = []
            for task in records:
                sha = task['task_commit']
                if _git(clone, 'checkout', '--quiet', '--detach',
                        sha).returncode != 0:
                    print(f"EVAL ERROR: checkout {sha} failed",
                          file=sys.stderr)
                    return 1
                task['_root'] = str(clone)
                base = task['base_commit'] or empty
                entry: dict[str, Any] = {
                    'task_id': task['id'], 'condition': condition,
                    'base': base, 'task_commit': sha,
                }

                # SCOPE replay (condition-independent)
                t0 = time.monotonic()
                try:
                    resolved = scope_resolver.resolve(clone, base=base)
                except scope_resolver.ScopeError as exc:
                    print(f'EVAL ERROR: scope {task["id"]}: {exc}',
                          file=sys.stderr)
                    return 1
                entry['scope_ms'] = int((time.monotonic() - t0) * 1000)
                scope_class, uncertain = classify_scope(
                    resolved['scope'], resolved['status'],
                    task['ground_truth_scope'])
                entry.update({
                    'scope_observed': resolved['scope'],
                    'scope_expected': task['ground_truth_scope'],
                    'scope_class': scope_class,
                    'scope_uncertain': uncertain,
                    'scope_status': resolved['status'],
                    'checks_selected': resolved['checks'],
                })

                # VERIFICATION replay (condition-independent)
                if args.skip_verification:
                    entry['verification'] = None
                else:
                    checks = check_runner.run(clone, resolved)
                    entry['verification'] = {
                        'checks': checks,
                        'verification_time_ms': int(sum(
                            c.get('duration_ms', 0) for c in checks)),
                    }

                # ORIENT (conditions B/C)
                if condition in ('orient', 'orient_mem0'):
                    t0 = time.monotonic()
                    orient = project_map.build(clone, 'quick',
                                               use_cache=False)
                    orient_ms = int((time.monotonic() - t0) * 1000)
                    exposure = orient_exposure(
                        orient, task['ground_truth_source'],
                        task['ground_truth_tests'])
                    entry['orient'] = {**exposure, 'orient_ms': orient_ms}
                else:
                    orient = None

                # MEM0 controlled experiment (condition C only)
                if condition == 'orient_mem0':
                    assert orient is not None  # set by the condition above
                    backend_resolved = memory.resolve_backend(clone)
                    if not backend_resolved.available:
                        entry['mem0'] = {
                            'available': False,
                            'reason': backend_resolved.reason,
                        }
                    else:
                        lessons = [t['historical_lesson'] for t in records
                                   if t.get('historical_lesson')]
                        idx = lessons.index(task['historical_lesson']) \
                            if task.get('historical_lesson') else 0
                        distractor = lessons[
                            (idx + 1) % len(lessons)]
                        t0 = time.monotonic()
                        probe = mem0_probe(task, orient, distractor,
                                           backend_resolved)
                        probe['probe_ms'] = int(
                            (time.monotonic() - t0) * 1000)
                        probe['available'] = True
                        entry['mem0'] = probe

                entries.append(entry)

            agg = aggregate(entries, condition, len(records))
            run_id = f'{base_id}_{condition}'
            run = {
                'schema': SCHEMA, 'run_id': run_id, 'date': date,
                'condition': condition, 'commit': head, 'tree_hash': tree,
                'task_set': task_sha, 'task_count': len(records),
                'metrics': agg['metrics'], 'counts': agg['counts'],
                'unavailable_reasons': agg['unavailable_reasons'],
                'tasks': entries,
                'notes': [
                    ('scope + verification replays are '
                     'condition-independent (deterministic git + tool '
                     'execution, not agent runs)'),
                    ('verification executed under current tool '
                     "binaries against each era's own config files"),
                    ('agent-layer metrics are null with reasons; never '
                     'zero-filled'),
                    ('classifications, exposure and retrieval decisions '
                     'are deterministic for a given task-set + commit; '
                     'wall-clock *_ms durations vary between runs'),
                ],
            }
            run_by_condition[condition] = run

        results_dir = Path(args.results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        for condition, run in run_by_condition.items():
            out = results_dir / f'{run["run_id"]}.json'
            out.write_text(json.dumps(run, indent=2, sort_keys=True)
                           + '\n', encoding='utf-8')
            print(f'wrote {out}')
        report = render_report(base_id, task_sha, len(records),
                               run_by_condition)
        md = results_dir / f'{base_id}.md'
        md.write_text(report, encoding='utf-8')
        print(f'wrote {md}')
    finally:
        shutil.rmtree(work, ignore_errors=True)
        # dataset records are annotated with an ephemeral run hook
        for task in records:
            task.pop('_root', None)
    return 0


if __name__ == '__main__':
    sys.exit(main())
