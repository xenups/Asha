#!/usr/bin/env python3
"""Live behavioral benchmark: A/B/C agent trajectories over the frozen
historical task set, executed by one pinned model, scored and reported
blinded.

Conditions (the ONLY intended difference is the injected context):

    A baseline      task prompt + normal tools + workspace at base_commit
    B +orient       ... + live orient --standard payload for that task
    C +orient_mem0  ... + orient + advisory Mem0 context from the Asha
                     memory adapter (retrieved records recorded verbatim;
                     distractors are recorded, never silently filtered)

Subcommands (STEP 22 order):

    python benchmarks/run_live.py prepare   # validate, freeze, payloads, seed
    python benchmarks/run_live.py run       # A -> B -> C, 3 replicates each
    python benchmarks/run_live.py score     # trajectories + verification -> metrics
    python benchmarks/run_live.py report    # blinded packages + aggregate md

Reuses scope_resolver / check_runner / project_map / memory verbatim; this
module orchestrates, normalizes and aggregates -- it does not re-implement
any Asha logic. Results land in benchmarks/results/live/ (gitignored).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / '.hermes' / 'tools'
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

RESULTS = REPO / 'benchmarks' / 'results'
# Cohorts never pool (STEP 17): live-v1 = codex/gpt-5.6-terra (inconclusive),
# live-v2 = its own result root, freeze, payloads and aggregates.
EXPERIMENT = os.environ.get('ASHA_BENCH_EXPERIMENT', 'live-v1')
LIVE = RESULTS / ('live' if EXPERIMENT == 'live-v1' else EXPERIMENT)
PAYLOADS = LIVE / '_payloads'
STORE = LIVE / '_store'
MAPPING = LIVE / '_mapping.json'
FREEZE = LIVE / '_freeze.json'
TASKS_FILE = REPO / 'benchmarks' / 'tasks.jsonl'

CONDITIONS = ('baseline', 'orient', 'orient_mem0')
REPLICATES = 3
# task-001 is excluded from LIVE execution: base_commit is the empty tree
# (re-implementing the whole repository from nothing is not a bounded task).
LIVE_EXCLUDED = {'task-001'}
# STEP 3: ONE runner and ONE model for all conditions of a cohort.
RUNNER = os.environ.get('ASHA_BENCH_RUNNER', 'codex')
# STEP 3/4: ONE runner, ONE model per cohort. live-v2 = opencode runner
# with the operator-selected deepseek flash 4 model (user directive);
# ASHA_BENCH_MODEL overrides for future cohorts only.
MODEL = os.environ.get(
    'ASHA_BENCH_MODEL',
    'opencode/deepseek-v4-flash' if RUNNER == 'opencode'
    else 'gpt-5.6-terra')
COMPLETION_EVENT = {'codex': 'turn.completed', 'opencode': 'text'}
RUN_TIMEOUT_S = 900
CONCURRENCY = 4

# STEP 1 field aliases: the dataset keeps its ground-truth provenance; the
# benchmark consumes these documented aliases instead of renaming anything.
ALIASES = {
    'ground_truth': 'ground_truth_outcome',
    'expected_source_of_truth': 'ground_truth_source',
    'expected_test_scope': 'ground_truth_tests',
    'expected_relevant_files': 'ground_truth_source+ground_truth_tests',
    'expected_outcome': 'ground_truth_outcome',
    'historical_lessons': 'historical_lesson',
}

CONFIG_MARKERS = (
    'pyproject.toml', 'ruff.toml', 'mypy.ini', '.mypy.ini', '.gitignore',
    'setup.cfg', 'tox.ini', '.python-version', '.tool-versions',
)
NAV_RE = re.compile(
    r'^\s*(?:"[^"]*")?\s*(ls|dir|tree|find|Get-ChildItem|git\s+(status|log|'
    r'diff|ls-files|show|branch)|rg\s+--files)\b', re.IGNORECASE)
SECRET_RES = [
    re.compile(r'sk-[A-Za-z0-9_-]{16,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'ghp_[A-Za-z0-9]{36}'),
    re.compile(r'Bearer\s+[A-Za-z0-9._-]{12,}'),
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    re.compile(r'(password|token|secret|api[_-]?key)\s*[=:]\s*\S{6,}',
               re.IGNORECASE),
]

BASE_PROMPT = """You are an autonomous software engineering agent working in a git repository checkout.

TASK
{task_description}

REQUIREMENTS
- Implement the change described by TASK directly in this repository.
- Do not weaken, skip, or delete existing tests to make failures disappear.
- Do not modify files outside this repository.
- When the implementation is complete, produce a brief final summary and stop.
"""

ORIENT_SECTION = """
--- ASHA ORIENT (standard mode; current repository facts, authoritative) ---
{orient_json}
--- END ORIENT ---
"""

MEMORY_SECTION = """
--- ASHA MEMORY CONTEXT (advisory historical context; NOT repository truth) ---
{memory_json}
--- END MEMORY ---

If any memory above conflicts with what you can observe in the
repository right now, the repository observation wins.
"""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def short(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]


def redact(text: str) -> str:
    for pattern in SECRET_RES:
        text = pattern.sub('[REDACTED]', text)
    return text


def load_tasks(path: Path = TASKS_FILE) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding='utf-8').splitlines() if line.strip()]


def live_tasks(tasks: list[dict] | None = None) -> list[dict]:
    records = tasks if tasks is not None else load_tasks()
    return [t for t in records if t['id'] not in LIVE_EXCLUDED]


def gt_sets(task: dict) -> tuple[set[str], set[str]]:
    source = set(task['ground_truth_source'])
    tests = set(task.get('ground_truth_tests') or [])
    return source, tests


def is_config_path(rel: str) -> bool:
    name = Path(rel).name
    return (rel.startswith('.github/') or name in CONFIG_MARKERS
            or name.endswith(('.md', '.rst', '.lock', '.nix', '.toml',
                              '.ini', '.cfg')))


def build_prompt(task: dict, condition: str) -> str:
    prompt = BASE_PROMPT.format(task_description=task['task_description'])
    if condition in ('orient', 'orient_mem0'):
        payload = read_json(PAYLOADS / f"{task['id']}_orient.json")
        prompt += ORIENT_SECTION.format(
            orient_json=json.dumps(payload, indent=2, sort_keys=True))
    if condition == 'orient_mem0':
        payload = read_json(PAYLOADS / f"{task['id']}_memory.json")
        prompt += MEMORY_SECTION.format(
            memory_json=json.dumps(payload, indent=2, sort_keys=True))
    return prompt


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def workspace_for(template: Path, run_dir: Path) -> Path:
    workspace = run_dir / 'repo'
    shutil.copytree(template, workspace, symlinks=True)
    return workspace


def file_inventory(workspace: Path) -> list[str]:
    proc = subprocess.run(['git', 'ls-files'], cwd=workspace,
                          capture_output=True, text=True, timeout=60)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def paths_in_command(command: str, inventory: list[str],
                     workspace: Path) -> list[str]:
    hits: list[str] = []
    for rel in inventory:
        if rel in command or str(workspace / rel) in command:
            hits.append(rel)
    return hits


def normalize_events(raw_events: list[dict], workspace: Path,
                     inventory: list[str]) -> list[dict]:
    """Codex JSONL -> flat events with harness arrival times (STEP 5/9).
    Wall-clock t_rel_ms is labeled harness arrival latency; the primary
    timing measures are event-based."""
    events: list[dict] = []
    t0: float | None = None
    for index, raw in enumerate(raw_events):
        arrived = raw.get('_t_arrival')
        if t0 is None and arrived is not None:
            t0 = arrived
        item = raw.get('item') or {}
        kind = item.get('type')
        files: list[str] = []
        action = None
        if kind == 'file_change':
            for change in item.get('changes') or []:
                path = str(change.get('path', '')).replace('\\', '/')
                prefix = str(workspace).replace('\\', '/').rstrip('/')
                if path.startswith(prefix + '/'):
                    path = path[len(prefix) + 1:]
                files.append(path)
            action = 'write'
        elif kind == 'command_execution':
            command = item.get('command') or ''
            files = paths_in_command(command, inventory, workspace)
            action = 'nav' if NAV_RE.match(command) else (
                'read' if files else 'command')
        events.append({
            'event_index': index,
            'timestamp': arrived,
            't_rel_ms': (int((arrived - t0) * 1000)
                         if t0 is not None and arrived else None),
            'event_type': kind or raw.get('type'),
            'action': action,
            'tool_name': ('file_change' if kind == 'file_change'
                          else 'command_execution'
                          if kind == 'command_execution' else None),
            'tool_arguments': redact(item.get('command') or '') if
            kind == 'command_execution' else None,
            'tool_result_summary': redact(str(item.get('aggregated_output')
                                              or item.get('text') or ''))[:400],
            'exit_code': item.get('exit_code'),
            'files_touched': files,
        })
    return events


def _rel_path(path: str, workspace: Path) -> str:
    clean = str(path).replace('\\', '/').strip()
    prefix = str(workspace).replace('\\', '/').rstrip('/')
    if clean.startswith(prefix + '/'):
        return clean[len(prefix) + 1:]
    return clean


def normalize_opencode(raw_events: list[dict], workspace: Path,
                       inventory: list[str]) -> list[dict]:
    """opencode `run --format json` -> the same flat event shape as the
    codex normalizer. t_rel_ms remains harness arrival latency (STEP 12:
    provider lines carry no authoritative cross-event clock contract we
    are willing to rely on); event counts stay primary."""
    events: list[dict] = []
    t0: float | None = None
    for index, raw in enumerate(raw_events):
        arrived = raw.get('_t_arrival')
        if t0 is None and arrived is not None:
            t0 = arrived
        part = raw.get('part') or {}
        kind = raw.get('type')
        files: list[str] = []
        action: str | None = None
        tool_name: str | None = None
        arguments: str | None = None
        summary = ''
        exit_code: int | None = None
        if kind == 'tool_use':
            tool_name = str(part.get('tool'))
            state = part.get('state') or {}
            inp = state.get('input') or {}
            arguments = redact(str(inp.get('command') or tool_name))
            summary = redact(str(state.get('output') or ''))[:400]
            exit_code = (state.get('metadata') or {}).get('exit')
            if tool_name == 'bash':
                command = str(inp.get('command') or '')
                files = paths_in_command(command, inventory, workspace)
                action = ('nav' if NAV_RE.match(command)
                          else ('read' if files else 'command'))
            elif tool_name in ('write', 'edit', 'patch'):
                target = _rel_path(
                    str(inp.get('filePath') or inp.get('path') or ''),
                    workspace)
                if target:
                    files = [target]
                action = 'write'
            else:  # read/grep/glob/... : substantive only if it names
                target = _rel_path(               # a repository file
                    str(inp.get('filePath') or inp.get('path') or ''),
                    workspace)
                if target and target in inventory:
                    files = [target]
                    action = 'read'
        elif kind == 'step_finish':
            summary = (f"cost={part.get('cost')} "
                       f"tokens={part.get('tokens')}")
        elif kind == 'text':
            summary = redact(str(part.get('text') or ''))[:400]
        events.append({
            'event_index': index,
            'timestamp': arrived,
            't_rel_ms': (int((arrived - t0) * 1000)
                         if t0 is not None and arrived else None),
            'event_type': kind,
            'action': action,
            'tool_name': tool_name,
            'tool_arguments': arguments,
            'tool_result_summary': summary,
            'exit_code': exit_code,
            'files_touched': files,
        })
    return events


def classify_action(event: dict, gt_source: set[str],
                    gt_tests: set[str]) -> str:
    """STEP 6 classes from independent ground truth.

    Rule (reported as an annotation rule, not ground truth itself):
    GT source/tests hit -> directly_relevant; code outside GT first ->
    wrong_direction (conservative: no per-task wrong-path list exists);
    config/docs hit -> useful_orientation; bare repo navigation ->
    useful_orientation; anything else -> irrelevant_exploration."""
    if event.get('action') in (None, 'command'):
        if event.get('action') == 'command':
            return 'irrelevant_exploration'
        return ''
    files = event.get('files_touched') or []
    if set(files) & (gt_source | gt_tests):
        return 'directly_relevant'
    if event.get('action') == 'nav':
        return 'useful_orientation'
    if any(f.endswith(('.py', '.sh', '.ps1')) for f in files):
        return 'wrong_direction'
    if any(is_config_path(f) for f in files):
        return 'useful_orientation'
    return 'irrelevant_exploration' if files else 'useful_orientation'


def score_trajectory(events: list[dict], task: dict) -> dict[str, Any]:
    """STEP 6-10 metrics from a normalized trajectory (deterministic)."""
    gt_source, gt_tests = gt_sets(task)
    gt_all = gt_source | gt_tests
    actions = [e for e in events if e.get('action')]
    if not actions:
        return {
            'first_action_class': None,
            'first_action_correct': None,
            'first_source_correct': None,
            'observed_source': None,
            'correction_event_index': None,
            'note': 'no substantive repository action recorded',
        }
    first = actions[0]
    first_class = classify_action(first, gt_source, gt_tests)
    observed = (first.get('files_touched') or [None])[0]
    first_source_correct = bool(set(first.get('files_touched') or []) & gt_all)

    correction_index = None
    if not first_source_correct:
        for event in actions[1:]:
            if set(event.get('files_touched') or []) & gt_all:
                correction_index = event['event_index']
                break

    reads = [e for e in actions if e.get('action') == 'read']
    writes = [e for e in actions if e.get('action') == 'write']
    wasted = sorted({f for e in reads for f in e['files_touched']
                     if f not in gt_all and not is_config_path(f)})

    first_relevant = next(
        (e for e in actions
         if set(e['files_touched'] or []) & gt_all), None)
    first_test = next(
        (e for e in actions
         if set(e['files_touched'] or []) & gt_tests), None)
    first_change_gt = next(
        (e for e in writes if set(e['files_touched'] or []) & gt_source),
        None)
    before_correct = 0 if first_relevant is None else sum(
        1 for e in actions if e['event_index'] < first_relevant['event_index'])
    wrong_edits = 0
    if correction_index is not None:
        wrong_edits = sum(
            1 for e in writes
            if e['event_index'] < correction_index
            and not set(e['files_touched'] or []) & gt_all)

    return {
        'first_action_class': first_class,
        'first_action_correct': first_class == 'directly_relevant',
        'first_source_correct': first_source_correct,
        'expected_source': sorted(gt_source),
        'observed_source': observed,
        'correction_event_index': correction_index,
        'tool_calls_before_correct_direction': (
            None if first_relevant is None else before_correct),
        'irrelevant_tool_calls': sum(
            1 for e in actions
            if e.get('_class') == 'irrelevant_exploration'),
        'files_read_before_correct_direction': (
            None if first_relevant is None else len({
                f for e in reads
                if e['event_index'] < first_relevant['event_index']
                for f in e['files_touched']})),
        'unique_files_read': len({f for e in reads
                                  for f in e['files_touched']}),
        'wasted_reads': wasted,
        'recovery_count': int(correction_index is not None),
        'recovery_tool_calls': (
            None if correction_index is None else sum(
                1 for e in actions
                if e['event_index'] < correction_index)),
        'wrong_edits_before_recovery': wrong_edits,
        'time_to_first_relevant_file_ms': (
            None if first_relevant is None else first_relevant['t_rel_ms']),
        'time_to_first_relevant_test_ms': (
            None if first_test is None else first_test['t_rel_ms']),
        'time_to_first_correct_hypothesis_ms': (
            None if first_relevant is None else first_relevant['t_rel_ms']),
        'time_to_first_correct_change_ms': (
            None if first_change_gt is None else first_change_gt['t_rel_ms']),
        'events_to_correct_hypothesis': (
            None if first_relevant is None
            else first_relevant['event_index']),
        'events_to_first_relevant_file': (
            None if first_relevant is None
            else first_relevant['event_index']),
        'tool_call_count': len(actions),
    }


def distribution(
        values: list[float | int | None]) -> dict[str, Any] | None:
    """STEP 19: count/mean/median/min/max -- null when nothing measured.
    None entries are dropped, never coerced to 0."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    ordered = sorted(clean)
    mid = len(ordered) // 2
    median = (ordered[mid] if len(ordered) % 2
              else (ordered[mid - 1] + ordered[mid]) / 2)
    return {
        'count': len(clean),
        'mean': round(sum(clean) / len(clean), 2),
        'median': median,
        'min': ordered[0],
        'max': ordered[-1],
    }


def rate(values: list[bool | None]) -> dict[str, Any]:
    measured = [v for v in values if v is not None]
    hits = sum(1 for v in measured if v)
    if not measured:
        return {'k': None, 'N': 0, 'value': None,
                'unavailable': 'no measured trials'}
    return {'k': hits, 'N': len(measured),
            'value': round(hits / len(measured), 3)}


# ---------------------------------------------------------------- prepare --

def cmd_prepare(args: argparse.Namespace) -> int:
    tasks = load_tasks()
    live = live_tasks(tasks)
    if not live:
        print('LIVE ERROR: no live tasks after exclusions', file=sys.stderr)
        return 1
    records_ok = all(
        {'id', 'task_description', 'base_commit', 'task_commit',
         'ground_truth_source', 'ground_truth_scope',
         'ground_truth_outcome'} <= set(t) for t in live)
    if not records_ok:
        print('LIVE ERROR: dataset fields missing', file=sys.stderr)
        return 1
    # base commits must exist; no empty-tree bases in the live set
    for task in live:
        if task['base_commit'] is None:
            print(f"LIVE ERROR: {task['id']} has empty-tree base",
                  file=sys.stderr)
            return 1
        proc = subprocess.run(
            ['git', 'cat-file', '-e', f"{task['base_commit']}^{{commit}}"],
            cwd=REPO, capture_output=True, timeout=60)
        if proc.returncode != 0:
            print(f"LIVE ERROR: {task['id']} base missing",
                  file=sys.stderr)
            return 1

    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=REPO,
                          capture_output=True, text=True,
                          timeout=60).stdout.strip()
    tree = subprocess.run(['git', 'rev-parse', 'HEAD^{tree}'], cwd=REPO,
                          capture_output=True, text=True,
                          timeout=60).stdout.strip()
    dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=REPO,
                           capture_output=True, text=True,
                           timeout=60).stdout.strip()
    if dirty:
        print('LIVE ERROR: working tree dirty; freeze refused',
              file=sys.stderr)
        return 1
    runner_version = subprocess.run(
        [RUNNER, '--version'], capture_output=True, text=True,
        timeout=60).stdout.strip()
    previous: dict[str, Any] | None = None
    if EXPERIMENT != 'live-v1':
        for old in sorted(RESULTS.glob('live/*.json')):
            if old.name.startswith('_'):
                continue
            try:
                blob = read_json(old)
            except Exception:  # noqa: S112 -- skip unrelated json files
                continue
            if blob.get('status') == 'inconclusive':
                previous = {'path': str(old.relative_to(REPO)),
                            'status': blob.get('status'),
                            'sha256': sha256_file(old)}
                break
    freeze: dict[str, Any] = {
        'schema': 2,
        'experiment_version': EXPERIMENT,
        'asha_commit': head,
        'asha_tree_hash': tree,
        'task_set_version': sha256_file(TASKS_FILE),
        'live_task_ids': [t['id'] for t in live],
        'excluded': {k: 'empty-tree base: repository re-implementation '
                        'is not a bounded task'
                     for k in sorted(LIVE_EXCLUDED)},
        'conditions': list(CONDITIONS),
        'tasks': len(live),
        'planned_trajectories': len(live) * len(CONDITIONS) * REPLICATES,
        'model': MODEL,
        'model_version': MODEL,
        'runner': RUNNER,
        'runner_version': runner_version,
        'provider': ('opencode-zen' if RUNNER == 'opencode'
                     else 'openai'),
        'system_prompt': (
            'opencode run built-in (identical across conditions)'
            if RUNNER == 'opencode' else
            'codex exec built-in (identical across conditions; '
            '--ignore-user-config not used)'),
        'timeout_s': RUN_TIMEOUT_S,
        'replicates': REPLICATES,
        'aliases': ALIASES,
        'frozen_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    PAYLOADS.mkdir(parents=True, exist_ok=True)

    import project_map  # reuse: orientation payloads (STEP 2)

    for task in live:
        template = LIVE / '_templates' / task['id']
        if not template.exists():
            template.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(['git', 'clone', '--quiet', str(REPO),
                            str(template)], cwd=REPO, check=True,
                           timeout=300)
            subprocess.run(['git', 'checkout', '--quiet', '--detach',
                            task['base_commit']], cwd=template, check=True,
                           timeout=120)
        orient = project_map.build(template, 'standard', use_cache=False)
        (PAYLOADS / f"{task['id']}_orient.json").write_text(
            json.dumps(orient, indent=2, sort_keys=True), encoding='utf-8')

    # condition-C payloads come from the frozen seed + the same orient
    seed_mem0(live, orient_payloads=True)
    for task in live:
        orient = read_json(PAYLOADS / f"{task['id']}_orient.json")
        payload = memory_payload(task, orient)
        (PAYLOADS / f"{task['id']}_memory.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding='utf-8')
    freeze['payload_hashes'] = {
        p.name: sha256_file(p) for p in sorted(PAYLOADS.glob('*.json'))}
    if previous is not None:
        freeze['previous_cohort'] = previous
    FREEZE.write_text(json.dumps(freeze, indent=2, sort_keys=True),
                      encoding='utf-8')
    print(json.dumps({'frozen': freeze['asha_commit'][:12],
                      'live_tasks': len(live),
                      'trajectories_target':
                          len(live) * len(CONDITIONS) * REPLICATES,
                      'model': MODEL}, indent=2))
    return 0


# ------------------------------------------------------------ mem0 seed --

# One deliberate distractor (workflow preference) plus a controlled
# stale repository fact (the precedence probe) join the useful lessons.
DISTRACTOR_LESSON = (
    'prefer conventional commits and focused pytest during development; '
    'unrelated to any single repository task')
CONTROL_CONFLICT = {'fact_key': 'stack.language', 'fact_value': 'javascript'}


def seed_mem0(live: list[dict], orient_payloads: bool) -> None:
    """Frozen benchmark store, rebuilt from the dataset every prepare:
    all historical lessons (useful for their own task) + one deliberate
    distractor + one stale repository fact (precedence probe)."""
    import memory

    if STORE.exists():
        shutil.rmtree(STORE, ignore_errors=True)
    STORE.mkdir(parents=True, exist_ok=True)
    backend = memory.resolve_backend(STORE)
    if not backend.available:
        print(f'LIVE ERROR: mem0 seed unavailable: {backend.reason}',
              file=sys.stderr)
        raise SystemExit(1)
    template_root = None
    if live:
        template_root = LIVE / '_templates' / live[0]['id']
    root = template_root if template_root and template_root.exists() \
        else REPO
    for task in live:
        lesson = task.get('historical_lesson')
        if lesson:
            memory.add_memory(root, lesson, 'historical_lesson',
                              backend=backend)
    memory.add_memory(root, DISTRACTOR_LESSON, 'workflow_preference',
                      backend=backend)
    memory.add_memory(root,
                      'repository uses python as its primary language',
                      'repository_fact',
                      fact_key=CONTROL_CONFLICT['fact_key'],
                      fact_value=CONTROL_CONFLICT['fact_value'],
                      backend=backend)


def memory_payload(task: dict, orient: dict) -> dict:
    """Condition-C context, produced by the current Asha memory adapter."""
    import memory

    backend = memory.resolve_backend(STORE)
    root = LIVE / '_templates' / task['id']
    results = memory.search_memory(root, task['task_description'],
                                   limit=8, backend=backend)
    records = memory.get_all_memories(root, backend=backend)
    context = memory.build_context(orient, records)
    retrieved = []
    for record in results:
        meta = record.get('metadata') or {}
        retrieved.append({
            'id': record['id'],
            'category': meta.get('category'),
            'content': record.get('content'),
            'status': meta.get('status'),
            'metadata': meta,
        })
    return {
        'retrieved': retrieved,
        'stale_repository_facts':
            context['memory']['stale_repository_facts'],
        'current_facts_language':
            (context['current_facts'].get('stack') or {}).get('language'),
        'seed_total': len(records),
        'tree_binding': orient.get('repo', {}).get('tree_hash'),
        'tree_binding_note': 'orient payload tree at base_commit; retrieved '
                             'records carry their own recorded_at/status',
    }


# ------------------------------------------------------------------- run --

def run_one(task: dict, condition: str, replicate: int,
            mapping: dict[str, Any]) -> dict:
    anon = hashlib.sha256(
        f"{task['id']}|{condition}|{replicate}|{time.time_ns()}"
        .encode()).hexdigest()[:12]
    run_dir = LIVE / 'runs' / anon
    run_dir.mkdir(parents=True, exist_ok=True)
    template = LIVE / '_templates' / task['id']
    workspace = workspace_for(template, run_dir)
    prompt = build_prompt(task, condition)
    (run_dir / 'prompt.txt').write_text(prompt, encoding='utf-8')

    if RUNNER == 'opencode':
        # non-interactive, structured transcript, same sandboxed workspace
        cmd = ['opencode', 'run', '-m', MODEL, '--format', 'json', prompt]
        prompt_as_arg = True
    else:
        cmd = ['codex', 'exec', '--json', '--color', 'never', '--ephemeral',
               '-C', str(workspace), '-s', 'workspace-write', '-m', MODEL,
               '-o', str(run_dir / 'last_message.txt'), '-']
        prompt_as_arg = False
    started = time.time()
    raw: list[dict] = []
    timed_out = False
    proc = subprocess.Popen(cmd, cwd=workspace,
                            stdin=(subprocess.DEVNULL if prompt_as_arg
                                   else subprocess.PIPE),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            encoding='utf-8', errors='replace')
    try:
        if not prompt_as_arg:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line or not line.startswith('{'):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event['_t_arrival'] = time.time()
            raw.append(event)
        proc.wait(timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait(timeout=30)
    stderr_tail = ''
    if proc.stderr is not None:
        stderr_tail = proc.stderr.read()[-2000:]
    elapsed_ms = int((time.time() - started) * 1000)

    inventory = file_inventory(workspace)
    if RUNNER == 'opencode':
        events = normalize_opencode(raw, workspace, inventory)
        texts = [e['tool_result_summary'] for e in events
                 if e.get('event_type') == 'text' and
                 e.get('tool_result_summary')]
        (run_dir / 'last_message.txt').write_text(
            texts[-1] if texts else '', encoding='utf-8')
        cost_usd = sum(c for c in
                       [(r2.get('part') or {}).get('cost')
                        for r2 in raw
                        if r2.get('type') == 'step_finish']
                       if isinstance(c, (int, float))) or None
    else:
        events = normalize_events(raw, workspace, inventory)
        cost_usd = None
    mapping[anon] = {'task_id': task['id'], 'condition': condition,
                     'replicate': replicate}
    record = {
        'schema': 1,
        'run_id': anon,
        'task_id': task['id'],
        'condition': condition,
        'task_set_version': sha256_file(TASKS_FILE),
        'asha_commit': None,   # filled by score from _freeze.json
        'asha_tree_hash': None,
        'model': MODEL,
        'model_version': MODEL,
        'replicate': replicate,
        'base_commit': task['base_commit'],
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'wall_clock_ms': elapsed_ms,
        'timed_out': timed_out,
        'exit_code': proc.returncode,
        'runner': RUNNER,
        'cost_usd': cost_usd,
        'stderr_tail': redact(stderr_tail),
        'metrics': {},
        'trajectory': {'events': events},
        'evaluation': {},
    }
    (run_dir / 'run.json').write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding='utf-8')
    return record


def cmd_run(args: argparse.Namespace) -> int:
    if not FREEZE.exists():
        print('LIVE ERROR: run prepare first', file=sys.stderr)
        return 1
    freeze = read_json(FREEZE)
    live = [t for t in live_tasks() if t['id']
            in freeze['live_task_ids']]
    mapping: dict[str, Any] = {}
    if MAPPING.exists():
        mapping = read_json(MAPPING)
    conditions = [args.condition] if args.condition else CONDITIONS
    for condition in conditions:  # STEP 22 order: A -> B -> C
        jobs = [(t, condition, r + 1) for t in live
                for r in range(freeze['replicates'])]
        print(f'RUN: {condition}: {len(jobs)} trajectories '
              f'(concurrency={CONCURRENCY})', flush=True)
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=CONCURRENCY) as pool:
            futures = [pool.submit(run_one, t, c, r, mapping)
                       for t, c, r in jobs]
            for future in concurrent.futures.as_completed(futures):
                record = future.result()
                print(f"  done {record['run_id']} {record['task_id']} "
                      f"{record['condition']} r{record['replicate']} "
                      f"exit={record['exit_code']} "
                      f"timeout={record['timed_out']} "
                      f"events={len(record['trajectory']['events'])}",
                      flush=True)
                MAPPING.write_text(json.dumps(mapping, indent=2,
                                              sort_keys=True),
                                   encoding='utf-8')
    print('RUN COMPLETE', flush=True)
    return 0


# ----------------------------------------------------------------- score --

def is_valid_run(record: dict[str, Any]) -> bool:
    """A trajectory counts only when the runner finished the turn:
    exit 0, no timeout, turn.completed present. Everything else is
    marked invalid and excluded from primary results (STEP 1/2)."""
    if record.get('exit_code') != 0 or record.get('timed_out'):
        return False
    types = [e.get('event_type')
             for e in record.get('trajectory', {}).get('events', [])]
    completion = COMPLETION_EVENT.get(
        str(record.get('runner') or 'codex'), 'turn.completed')
    return completion in types


QUOTA_PATTERNS = ('usage limit', 'quota', 'rate limit',
                  'rate_limit', 'too many requests', '429')
AUTH_PATTERNS = ('401', 'unauthorized', 'authentication',
                 'invalid api key', 'login required', 'not logged in')
ENV_PATTERNS = ('enoent', 'spawn', 'access is denied',
                'not a git repository', 'unable to read file')


def classify_failure(record: dict[str, Any]) -> str:
    """STEP 11 taxonomy; a runner failure is never agent failure."""
    if is_valid_run(record):
        return 'valid'
    if record.get('timed_out'):
        return 'timeout'
    blob = ' '.join([
        str(record.get('stderr_tail') or ''),
        ' '.join(str(e.get('tool_result_summary') or '')
                 for e in record.get('trajectory', {}).get('events', [])),
    ]).lower()
    if any(p in blob for p in QUOTA_PATTERNS):
        return 'quota_failure'
    if any(p in blob for p in AUTH_PATTERNS):
        return 'authentication_failure'
    if any(p in blob for p in ENV_PATTERNS):
        return 'environment_failure'
    types = [e.get('event_type')
             for e in record.get('trajectory', {}).get('events', [])]
    if record.get('exit_code') == 0 or not types:
        return 'invalid_protocol'
    return 'runner_failure'


def verify_workspace(workspace: Path, base: str) -> dict[str, Any]:
    """STEP 16: harness-side verification, measured separately from the
    agent's reasoning phase (reuse Asha's own engines)."""
    import check_runner
    import scope_resolver

    started = time.time()
    resolved = scope_resolver.resolve(workspace, base=base)
    checks = check_runner.run(workspace, resolved)
    return {
        'scope': resolved['scope'],
        'scope_status': resolved['status'],
        'checks': resolved['checks'],
        'check_results': [{'name': c['name'], 'status': c['status'],
                           'duration_ms': c.get('duration_ms'),
                           'output_tail': redact(
                               c.get('output_tail') or '')[:600]}
                          for c in checks],
        'verification_time_ms': int((time.time() - started) * 1000),
        'checks_executed': len(checks),
        'failed_checks': [c['name'] for c in checks
                          if c['status'] not in ('passed', 'skipped')],
        'retries': 0,
    }


def _score_one(run_path: Path, freeze: dict[str, Any],
               tasks: dict[str, dict], skip_verification: bool
               ) -> str:
    record = read_json(run_path)
    if not is_valid_run(record):
        types = [e.get('event_type')
                 for e in record['trajectory']['events']]
        klass = classify_failure(record)
        record['invalid'] = {
            'class': klass,
            'reason': (f'{klass}: runner-level failure, never agent '
                       f'failure (event_types={len(types)}, '
                       f'exit={record.get("exit_code")})'),
            'exit_code': record.get('exit_code'),
            'timed_out': record.get('timed_out'),
        }
        record['metrics'] = {}
        run_path.write_text(
            json.dumps(record, indent=2, sort_keys=True),
            encoding='utf-8')
        return (f"INVALID {record['run_id']} {record['task_id']} "
                f"{record['condition']} r{record['replicate']}: "
                f"{klass}")
    task = tasks[record['task_id']]
    events = record['trajectory']['events']
    # class labels for the irrelevant_tool_calls derivation
    gt_source, gt_tests = gt_sets(task)
    for event in events:
        if event.get('action'):
            event['_class'] = classify_action(
                event, gt_source, gt_tests)
    metrics = score_trajectory(events, task)
    workspace = run_path.parent / 'repo'
    if workspace.exists() and not skip_verification:
        metrics['verification'] = verify_workspace(
            workspace, task['base_commit'])
    metrics['final_correct'] = None  # blinded evaluation fills this
    metrics['final_correct_reason'] = 'pending_blinded_evaluation'
    record['metrics'] = metrics
    record['asha_commit'] = freeze['asha_commit']
    record['asha_tree_hash'] = freeze['asha_tree_hash']
    run_path.write_text(json.dumps(record, indent=2, sort_keys=True),
                        encoding='utf-8')
    return (f"scored {record['run_id']} {record['task_id']} "
            f"{record['condition']} r{record['replicate']} "
            f"first={metrics.get('first_action_class')}")


def cmd_score(args: argparse.Namespace) -> int:
    freeze = read_json(FREEZE)
    tasks = {t['id']: t for t in live_tasks()}
    run_paths = sorted((LIVE / 'runs').glob('*/run.json'))
    if not run_paths:
        print('LIVE ERROR: no runs to score', file=sys.stderr)
        return 1
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(_score_one, p, freeze, tasks,
                               args.skip_verification)
                   for p in run_paths]
        for future in concurrent.futures.as_completed(futures):
            print(future.result(), flush=True)
    print('SCORE COMPLETE')
    return 0


# ------------------------------------------------- blinded evaluation -----

def cmd_blind(args: argparse.Namespace | None = None) -> int:
    """STEP 12: anonymized judging packages. Condition labels and the
    private mapping never enter a judge package."""
    tasks = {t['id']: t for t in live_tasks()}
    judge_root = LIVE / '_judge'
    if judge_root.exists():
        shutil.rmtree(judge_root, ignore_errors=True)
    judge_root.mkdir(parents=True, exist_ok=True)
    for run_path in sorted((LIVE / 'runs').glob('*/run.json')):
        record = read_json(run_path)
        if record.get('invalid'):
            continue  # invalid trajectories are never judged
        task = tasks[record['task_id']]
        workspace = run_path.parent / 'repo'
        diff = ''
        if workspace.exists():
            base = task['base_commit']
            diff_proc = subprocess.run(
                ['git', 'diff', base], cwd=workspace,
                capture_output=True, text=True, timeout=120)
            status_proc = subprocess.run(
                ['git', 'status', '--porcelain'], cwd=workspace,
                capture_output=True, text=True, timeout=60)
            untracked = [
                line.split(None, 1)[1] for line
                in status_proc.stdout.splitlines()
                if line.startswith('?? ') and not line.endswith('/')]
            for path in untracked:
                target = workspace / path
                if target.is_file():
                    diff_proc.stdout += (
                        f'\n--- untracked new file: {path}\n'
                        + target.read_text(encoding='utf-8',
                                           errors='replace')[:8000])
            diff = diff_proc.stdout
        # Blinding fix (blocking defect): the correctness package must
        # not identify the condition. Memory shown to the agent lives in
        # a sidecar consumed only by the memory-impact evaluator.
        mem_payload = None
        if record['condition'] == 'orient_mem0':
            mem_payload = read_json(
                PAYLOADS / f"{record['task_id']}_memory.json")
            memory_dir = judge_root / '_memory_context'
            memory_dir.mkdir(parents=True, exist_ok=True)
            (memory_dir / f"{record['run_id']}.json").write_text(
                json.dumps({'anon_id': record['run_id'],
                            'memory_context': mem_payload},
                           indent=2, sort_keys=True),
                encoding='utf-8')
        package = {
            'anon_id': record['run_id'],
            'task_description': task['task_description'],
            'ground_truth': {
                'expected_source_of_truth':
                    task['ground_truth_source'],
                'expected_tests': task['ground_truth_tests'],
                'expected_outcome': task['ground_truth_outcome'],
                'known_failure_modes': task['known_failure_modes'],
                'historical_lesson': task.get('historical_lesson'),
                'expected_scope': task['ground_truth_scope'],
            },
            'agent_diff': redact(diff)[:120000],
            'verification': (record['metrics'].get('verification')
                             or {}),
            'last_message': redact(
                (run_path.parent / 'last_message.txt').read_text(
                    encoding='utf-8', errors='replace')
                if (run_path.parent / 'last_message.txt').exists()
                else ''),
            'questions_for_evaluator': {
                'final': 'correct | partial | incorrect + short '
                         'evidence-based reason',
                'source_of_truth_reasoning': 'did the diff build on the '
                     'expected source of truth? (yes/no + reason)',
                'repeated_mistake': 'does the trajectory repeat any '
                     'known_failure_mode? (none | <mode>)',
            },
        }
        (judge_root / f"{record['run_id']}.json").write_text(
            json.dumps(package, indent=2, sort_keys=True),
            encoding='utf-8')
    count = len(list(judge_root.glob('*.json')))
    print(json.dumps({'blind_packages': count,
                      'judge_dir': str(judge_root),
                      'mapping_is_private': str(MAPPING)},
                     indent=2))
    return 0


def apply_judgements(args: argparse.Namespace) -> int:
    """Merge blinded verdicts back onto run records via _mapping."""
    verdicts = read_json(Path(args.verdicts))
    mapping = read_json(MAPPING)
    for anon, verdict in verdicts.items():
        meta = mapping.get(anon)
        if meta is None:
            print(f'WARN: unknown anon {anon}', file=sys.stderr)
            continue
        run_path = LIVE / 'runs' / anon / 'run.json'
        if not run_path.exists():
            continue
        record = read_json(run_path)
        record['evaluation'] = verdict
        final = str(verdict.get('final', '')).lower()
        tests_pass = not (record['metrics'].get('verification') or {}
                          ).get('failed_checks')
        source = bool(verdict.get('source_of_truth_reasoning', '')
                      .lower().startswith('yes'))
        record['metrics']['final_correct'] = bool(
            final.startswith('correct') and tests_pass and source)
        record['metrics']['final_correct_reason'] = verdict.get(
            'reason', final)
        run_path.write_text(json.dumps(record, indent=2, sort_keys=True),
                            encoding='utf-8')
    print('JUDGEMENTS APPLIED')
    return 0


# ------------------------------------------------------------- aggregate --

def aggregate(runs: list[dict], freeze: dict) -> dict[str, Any]:
    out: dict[str, Any] = {
        'schema': 1,
        'run_id': 'live_' + freeze['asha_commit'][:7] + '_' +
                  freeze['task_set_version'][:8],
        'conditions': list(CONDITIONS),
        'asha_commit': freeze['asha_commit'],
        'asha_tree_hash': freeze['asha_tree_hash'],
        'task_set_version': freeze['task_set_version'],
        'model': freeze['model'],
        'model_version': freeze['model_version'],
        'runner': freeze.get('runner'),
        'trajectory_count': len(runs),
        'per_condition': {},
        'mem0_analysis': {},
        'orient_analysis': {},
    }
    # STEP 0/15: honest reconciliation from the frozen manifest
    dataset = load_tasks()
    valid_runs = [r for r in runs if not r.get('invalid')]
    failed_runs = [r for r in runs if r.get('invalid')]
    tasks_planned = len(dataset)
    tasks_frozen = len(freeze['live_task_ids'])
    recon = {
        'planned_protocol': tasks_planned * 3 * 3,
        'tasks_planned': tasks_planned,
        'planned_frozen': tasks_frozen * 3 * 3,
        'tasks_frozen': tasks_frozen,
        'attempted': len(runs),
        'valid': len(valid_runs),
        'failed': len(failed_runs),
        'excluded': tasks_planned * 3 * 3 - tasks_frozen * 3 * 3,
        'excluded_reason': 'task-001 excluded at freeze (empty-tree '
                           'base; whole-repository re-implementation is '
                           'not a bounded task) -- proven from '
                           '_freeze.json live_task_ids/excluded',
        'task_set': freeze['task_set_version'],
    }
    failure_classes: dict[str, int] = {}
    for r in failed_runs:
        cls = (r.get('invalid') or {}).get('class') or 'unclassified'
        failure_classes[cls] = failure_classes.get(cls, 0) + 1
    attempted_conds = {r['condition'] for r in runs}
    valid_conds = {r['condition'] for r in valid_runs}
    zero_valid = sorted(attempted_conds - valid_conds)
    recon['failure_classes'] = failure_classes
    recon['failure_cause'] = (
        ('runner failure classes: '
         + ', '.join(f'{k}={v}' for k, v in
                     sorted(failure_classes.items())))
        if failure_classes else 'no runner failures')
    if not valid_runs:
        recon['status'] = (
            'inconclusive: 0 valid trajectories '
            f"({recon['failure_cause']})")
    elif zero_valid:
        recon['status'] = (
            'inconclusive (STEP 19 stop condition): valid only in '
            f"{sorted(valid_conds)}; zero-valid cells "
            f"{zero_valid} -- cohort unbalanced, never padded")
    else:
        recon['status'] = (
            'balanced cohort: every condition has valid trajectories; '
            'runner/model uniform across A/B/C')
    out.update({
        'planned_trajectories': recon['planned_protocol'],
        'frozen_trajectories': recon['planned_frozen'],
        'attempted_trajectories': recon['attempted'],
        'valid_trajectories': recon['valid'],
        'failed_trajectories': recon['failed'],
        'excluded_trajectories': recon['excluded'],
        'reconciliation': recon,
        'invalid_runs': [
            {'run_id': r['run_id'], 'task_id': r['task_id'],
             'condition': r['condition'], 'replicate': r['replicate'],
             'failure_class': (r.get('invalid') or {}).get('class'),
             'reason': (r.get('invalid') or {}).get('reason')}
            for r in failed_runs],
        'limitations': [
            (f"runner={freeze.get('runner')} model="
             f"{freeze.get('model')}: one runner/model per cohort, "
             'cohorts never pool'),
            ('0-valid cells are n/a, never zero'
             + (f" ({', '.join(zero_valid)})" if zero_valid else '')
             + '; invalid runs are never scored or judged'),
            (f"{recon['excluded']} protocol trajectories never "
             'attempted: task-001 excluded at freeze for empty-tree '
             'base'),
            ('timestamps are harness arrival times, not model-'
             'internal timings; event counts are primary'),
        ],
        'failure_classes': failure_classes,
    })
    for condition in CONDITIONS:
        attempted_group = [r for r in runs
                           if r['condition'] == condition]
        group = [r for r in attempted_group
                 if not r.get('invalid')]
        if attempted_group:
            out['per_condition'].setdefault(condition, {})
            out['per_condition'][condition]['attempted'] = len(
                attempted_group)
            out['per_condition'][condition]['invalid'] = len(
                attempted_group) - len(group)
        if not group:
            if attempted_group:
                out['per_condition'][condition]['trajectories'] = 0
            continue
        m = [r['metrics'] for r in group]
        ver = [x['verification'] for x in m if x.get('verification')]
        out['per_condition'][condition] = {
            **out['per_condition'].get(condition, {}),
            'trajectories': len(group),
            'valid': len(group),
            'timed_out': rate([bool(r.get('timed_out')) for r in group]),
            'final_correctness': rate([x.get('final_correct') for x in m]),
            'first_action_correctness': rate(
                [x.get('first_action_correct') for x in m]),
            'first_action_classes': {
                cls: rate([x.get('first_action_class') == cls for x in m])
                for cls in ('directly_relevant', 'useful_orientation',
                            'irrelevant_exploration', 'wrong_direction')},
            'source_of_truth_divergence': rate(
                [None if x.get('first_source_correct') is None
                 else not x['first_source_correct'] for x in m]),
            'events_to_correct_hypothesis': distribution(
                [x.get('events_to_correct_hypothesis') for x in m]),
            'time_to_first_relevant_file_ms': distribution(
                [x.get('time_to_first_relevant_file_ms') for x in m]),
            'recovery_count_total': sum(
                x.get('recovery_count') or 0 for x in m),
            'recovery_tool_calls': distribution(
                [x.get('recovery_tool_calls') for x in m]),
            'tool_call_count': distribution(
                [x.get('tool_call_count') for x in m]),
            'wasted_reads_total': sum(
                len(x.get('wasted_reads') or []) for x in m),
            'verification_time_ms': distribution(
                [v.get('verification_time_ms') for v in ver]),
            'checks_executed': distribution(
                [v.get('checks_executed') for v in ver]),
            'failed_checks_total': sum(
                len(v.get('failed_checks') or []) for v in ver),
            'scope_distribution': {
                level: sum(1 for v in ver if v.get('scope') == level)
                for level in ('S0', 'S1', 'S2', 'S3', 'S4')},
        }
    # STEP 13/14: memory analysis from condition C payloads + verdicts
    c_runs = [r for r in valid_runs
              if r['condition'] == 'orient_mem0']
    if c_runs:
        tasks = {t['id']: t for t in live_tasks()}
        retrieved_total = 0
        useful = stale = conflicting = irrelevant = 0
        harmful = 0
        pollution = {k: 0 for k in (
            'retrieved_not_used', 'retrieved_and_ignored',
            'retrieved_and_consumed', 'retrieved_and_caused_wrong_direction',
            'retrieved_and_caused_wrong_edit')}
        for run in c_runs:
            payload = read_json(
                PAYLOADS / f"{run['task_id']}_memory.json")
            own_task = tasks[run['task_id']]
            for item in payload['retrieved']:
                retrieved_total += 1
                if item.get('status') == 'stale':
                    stale += 1
                    continue
                if (item.get('category') == 'historical_lesson'
                        and item.get('content')
                        == own_task.get('historical_lesson')):
                    useful += 1
                elif item.get('category') == 'repository_fact':
                    conflicting += 1  # controlled conflict, always shown
                else:
                    irrelevant += 1
            for outcome, count in (run.get('evaluation', {})
                                   .get('memory_impact_counts')
                                   or {}).items():
                if outcome in pollution:
                    pollution[outcome] += count
                if outcome.startswith('retrieved_and_caused'):
                    harmful += count
        out['mem0_analysis'] = {
            'retrieved_total': retrieved_total,
            'useful_memories': useful,
            'irrelevant_memories': irrelevant,
            'stale_memories': stale,
            'conflicting_memories': conflicting,
            'harmful_memories': harmful,
            'retrieval_precision': (
                round(useful / retrieved_total, 3)
                if retrieved_total else None),
            'pollution_outcomes': pollution,
            'repeated_mistakes': {
                cond: rate([
                    str((r.get('evaluation') or {}).get(
                        'repeated_mistake', 'none')).lower() != 'none'
                    for r in valid_runs if r['condition'] == cond
                    and tasks[r['task_id']].get('historical_lesson')])
                for cond in ('orient', 'orient_mem0')
                if any(r['condition'] == cond
                       for r in runs)},
            'baseline_distractor_exclusion_visible': 'see tool-layer '
                'benchmarks/results/*.md distractor_excluded (3 / 9); '
                'behavioral impact measured here, retriever NOT tuned',
            'runs_with_impact_judgement': sum(
                1 for run in c_runs
                if run.get('evaluation', {}).get('memory_impact')),
            'judged_classes': {
                cls: sum(
                    1 for run in c_runs
                    for entry in (run.get('evaluation', {})
                                  .get('memory_impact') or [])
                    if entry.get('class') == cls)
                for cls in ('useful', 'neutral', 'irrelevant', 'harmful',
                            'stale', 'conflicting')},
            'judged_records': sum(
                len(run.get('evaluation', {}).get('memory_impact') or [])
                for run in c_runs),
            'memory_used': sum(
                c for run in c_runs
                for outcome, c in (run.get('evaluation', {})
                                   .get('memory_impact_counts')
                                   or {}).items()
                if outcome in ('retrieved_and_consumed',
                               'retrieved_and_caused_wrong_direction',
                               'retrieved_and_caused_wrong_edit')),
            'memory_harmful_behavior': sum(
                c for run in c_runs
                for outcome, c in (run.get('evaluation', {})
                                   .get('memory_impact_counts')
                                   or {}).items()
                if outcome.startswith('retrieved_and_caused')),
        }
    # STEP 18 orient analysis row block
    out['orient_analysis'] = {
        cond: {
            'first_action_correctness':
                out['per_condition'].get(cond, {}).get(
                    'first_action_correctness'),
            'source_of_truth_divergence':
                out['per_condition'].get(cond, {}).get(
                    'source_of_truth_divergence'),
            'events_to_correct_hypothesis':
                out['per_condition'].get(cond, {}).get(
                    'events_to_correct_hypothesis'),
            'recovery_count_total':
                out['per_condition'].get(cond, {}).get(
                    'recovery_count_total'),
        }
        for cond in CONDITIONS
        if cond in out['per_condition']}
    return out


# ---------------------------------------------------------------- report --

def _fmt(value: Any) -> str:
    if value is None:
        return 'n/a'
    if isinstance(value, dict):
        if 'k' in value:
            if value.get('N') in (None, 0):
                return '0 valid (n/a)'
            pct = round(100 * (value.get('value') or 0), 1)
            return f"{value['k']} / {value['N']} ({pct}%)"
        if 'count' in value:
            return (f"n={value['count']} mean={value['mean']} "
                    f"med={value['median']} min={value['min']} "
                    f"max={value['max']}")
        return json.dumps(value, sort_keys=True)
    return str(value)


def render_report(aggregate_data: dict[str, Any],
                  judged: bool) -> str:
    per = aggregate_data['per_condition']
    freeze = read_json(FREEZE)
    recon = aggregate_data['reconciliation']
    integrity = aggregate_data.get('cohort_integrity')
    zero_conds = sorted(
        c for c in CONDITIONS
        if per.get(c, {}).get('attempted')
        and not per.get(c, {}).get('trajectories'))
    evaluation_note = (
        'blinded verdicts merged' if judged
        else 'PENDING blinded evaluation')

    def cell_count(cond: str, key: str) -> str:
        return str(per.get(cond, {}).get(key, 0))

    lines = [
        '# Asha live agent benchmark -- ' + aggregate_data['run_id'],
        '',
        '## 1. Experiment configuration',
        '',
        (f"- Asha commit (frozen): `{aggregate_data['asha_commit']}` "
        f"(tree `{aggregate_data['asha_tree_hash'][:12]}`)"),
        (f"- model: {aggregate_data['model']} "
        f"({aggregate_data.get('runner')})"),
        (f"- timeout: {freeze['timeout_s']}s, replicates: "
         f"{freeze['replicates']} per task x condition"),
        ('- prompts: byte-identical across conditions except the '
         'injected ORIENT / MEMORY sections; no ground-truth leakage '
         '(asserted by tests and by prompt_sha256 recording)'),
        ('- event_time = harness arrival time (runner event streams '
         'are not an authoritative cross-event clock); event-based '
         'metrics are primary'),
        f"- evaluation: {evaluation_note}",
        '',
        ('## 2. Cohort integrity and reconciliation (protocol '
         f"{recon['planned_protocol']} / frozen "
         f"{recon['planned_frozen']} / valid {recon['valid']})"),
        '',
        (f"- planned (protocol): {recon['planned_protocol']} = "
         f"{recon['tasks_planned']} tasks x 3 conditions x 3 "
         'replicates'),
        (f"- planned (frozen manifest): {recon['planned_frozen']} = "
         f"{recon['tasks_frozen']} live tasks, task-set "
         f"{recon['task_set']}"),
        (f"- excluded at freeze: {recon['excluded']} = task-001 x 9 "
        '-- proven from _freeze.json: ' + recon['excluded_reason']),
        f"- attempted: {recon['attempted']}",
        f"- valid: {recon['valid']}",
        f"- failed: {recon['failed']} -- " + recon['failure_cause'],
        ('- failure classes: '
         + json.dumps(recon.get('failure_classes') or {},
                      sort_keys=True)),
        ("- reconciliation_status: " + recon['status']),
        ('- cohort integrity: '
         + (json.dumps(integrity, sort_keys=True)
            if integrity else 'not computed')),
        '',
        '### Dataset',
        '',
        (f"- {recon['tasks_frozen']} live historical tasks from "
         f"task-set {recon['task_set']} (one repository, own history)"),
        ('- ground truth: merged patch / git history / gate status '
         "at merge time / maintainer annotation -- never the "
         "agent's own result"),
        '',
        '### Conditions',
        '',
        '| Condition | Injected context | Attempted | Invalid | Valid |',
        '| --- | --- | --- | --- | --- |',
        ('| baseline | task + tools only | '
         + cell_count('baseline', 'attempted') + ' | '
         + cell_count('baseline', 'invalid') + ' | '
         + cell_count('baseline', 'trajectories') + ' |'),
        ('| +ORIENT | + orient --standard payload | '
         + cell_count('orient', 'attempted') + ' | '
         + cell_count('orient', 'invalid') + ' | '
         + cell_count('orient', 'trajectories') + ' |'),
        ('| +ORIENT+MEM0 | + advisory memory context | '
         + cell_count('orient_mem0', 'attempted') + ' | '
         + cell_count('orient_mem0', 'invalid') + ' | '
         + cell_count('orient_mem0', 'trajectories') + ' |'),
        '',
        '## 3. Final correctness and A/B/C results',
        '',
        '| Metric | Baseline | +ORIENT | +ORIENT+MEM0 |',
        '| --- | --- | --- | --- |',
    ]

    def row(label: str, key: str, kind: str = 'rate') -> None:
        cells = []
        for cond in CONDITIONS:
            block = per.get(cond, {})
            value = block.get(key)
            if value is None:
                cells.append('n/a')
            elif kind == 'dist':
                cells.append(_fmt(value))
            else:
                cells.append(_fmt(value))
        lines.append(f'| {label} | ' + ' | '.join(cells) + ' |')

    row('Final correctness', 'final_correctness')
    row('First-action correctness', 'first_action_correctness')
    row('Source-of-truth divergence', 'source_of_truth_divergence')
    row('Time-to-correct (events)', 'events_to_correct_hypothesis', 'dist')
    row('Time-to-correct (ms)', 'time_to_first_relevant_file_ms', 'dist')
    row('Recovery cost (tool calls)', 'recovery_tool_calls', 'dist')
    row('Recovery count (total)', 'recovery_count_total')
    row('Tool calls', 'tool_call_count', 'dist')
    row('Wasted reads (total)', 'wasted_reads_total')
    row('Verification cost (ms)', 'verification_time_ms', 'dist')
    row('Checks executed', 'checks_executed', 'dist')
    row('Failed checks (total)', 'failed_checks_total')
    row('Timed out', 'timed_out')
    lines += ['', 'First-action class breakdown (k / N):', '']
    lines += ['| Class | Baseline | +ORIENT | +ORIENT+MEM0 |',
              '| --- | --- | --- | --- |']
    for cls in ('directly_relevant', 'useful_orientation',
                'irrelevant_exploration', 'wrong_direction'):
        cells = [
            _fmt(per.get(cond, {}).get('first_action_classes',
                                       {}).get(cls))
            for cond in CONDITIONS]
        lines.append(f'| {cls} | ' + ' | '.join(cells) + ' |')

    lines += ['', '## 4. ORIENT results', '']
    for cond in CONDITIONS:
        block = per.get(cond) or {}
        valid_n = block.get('trajectories', 0)
        if not valid_n:
            lines.append(
                f'- {cond}: 0 valid trajectories -- all '
                f"{block.get('attempted', 0)} attempts failed at the "
                'runner level; every ORIENT metric n/a (never zero)')
            continue
        lines.append(
            f'- {cond} (N={valid_n}): first-action correct='
            f"{_fmt(block.get('first_action_correctness'))}; "
            'source-of-truth divergence='
            f"{_fmt(block.get('source_of_truth_divergence'))}; "
            'time-to-correct events='
            f"{_fmt(block.get('events_to_correct_hypothesis'))}; "
            'time-to-correct ms='
            f"{_fmt(block.get('time_to_first_relevant_file_ms'))}; "
            'recovery: count total='
            f"{block.get('recovery_count_total')}, tool calls="
            f"{_fmt(block.get('recovery_tool_calls'))}")

    mem = aggregate_data.get('mem0_analysis') or {}
    lines += ['', '## 5. Mem0 results (condition C)', '']
    if mem:
        for key in ('retrieved_total', 'useful_memories',
                    'irrelevant_memories', 'stale_memories',
                    'conflicting_memories', 'harmful_memories',
                    'judged_classes', 'judged_records',
                    'retrieval_precision', 'pollution_outcomes',
                    'repeated_mistakes'):
            blob = json.dumps(mem.get(key), sort_keys=True)
            lines.append(f'- {key}: `{blob}`')
        lines.append('- baseline distractor result stays visible: '
                     + str(mem.get(
                         'baseline_distractor_exclusion_visible')))
    else:
        lines.append('- 0 valid condition-C trajectories: retrieval '
                     'payloads exist in _payloads/*_memory.json but '
                     'used/useful/harmful behavioral rates are n/a')

    lines += ['', '## 6. Context pollution', '']
    if mem:
        blob = json.dumps(mem.get('pollution_outcomes'),
                          sort_keys=True)
        lines.append(f'- pollution outcomes: `{blob}`')
        lines.append('- retrieved != used; used != useful; useful != '
                     'causally responsible -- outcomes above come from '
                     'the blinded evaluator, not from retrieval counts')
    else:
        lines.append('- n/a (no valid condition-C trajectories); the '
                     'tool-layer 3 / 9 distractor-exclusion result '
                     'remains visible as retrieval behavior only')
    lines += ['', '## 7. Recovery cost', '']
    for cond in CONDITIONS:
        block = per.get(cond) or {}
        if block.get('trajectories'):
            lines.append(
                f"- {cond}: recovery count total="
                f"{block.get('recovery_count_total')}; tool calls="
                f"{_fmt(block.get('recovery_tool_calls'))}; wasted "
                f"reads total={block.get('wasted_reads_total')} "
                '(self-correction never scores like a right first '
                'decision)')
        else:
            lines.append(f'- {cond}: n/a (0 valid trajectories)')

    lines += ['', ('## 8. Verification cost (harness-side, separate '
              'from agent behavior)'), '']
    for cond in CONDITIONS:
        block = per.get(cond) or {}
        if block.get('trajectories'):
            lines.append(
                f"- {cond}: verification ms="
                f"{_fmt(block.get('verification_time_ms'))}; checks="
                f"{_fmt(block.get('checks_executed'))}; failed total="
                f"{block.get('failed_checks_total')} -- lower cost is "
                'not automatically better')
        else:
            lines.append(f'- {cond}: n/a (0 valid trajectories)')

    lines += ['', ('Scope (deterministic analysis, reported separately '
              'from reasoning -- STEP 17)'), '']
    for cond in CONDITIONS:
        block = per.get(cond, {})
        if block:
            lines.append(f"- {cond}: scope distribution "
                         f"`{json.dumps(block.get('scope_distribution'))}` "
                         '| agent reasoning metrics above')

    previous = freeze.get('previous_cohort')
    if previous:
        old = read_json(REPO / str(previous['path']))
        lines += ['', '## 9. Previous Inconclusive Cohort (not pooled)',
                  '',
                  ('- artifact: `' + str(previous['path'])
                   + '` (sha256 ' + previous['sha256'][:12] + ')'),
                  ('- status: ' + str(old.get('status')) + ' -- '
                   + str(old.get('reason'))),
                  ('- valid: '
                   + str(old.get('valid_trajectories'))
                   + ' baseline-only trajectories; '
                   + str(old.get('failed_trajectories'))
                   + ' runner failures; B/C cells 0 valid'),
                  ('- never pooled with this cohort: different '
                   'runner/model; no combined percentage exists '
                   '(STEP 17)'),
                  '']

    lines += ['## 10. Observed facts', '']
    for cond in CONDITIONS:
        block = per.get(cond, {})
        if not block:
            continue
        if not block.get('trajectories'):
            lines.append(
                f"- {cond}: 0 valid / {block.get('attempted', 0)} "
                'attempted -- no behavioral observation exists')
            continue
        lines.append(
            f"- {cond} (valid n={block['trajectories']}): "
            f"final_correct={_fmt(block['final_correctness'])}; "
            f"first-action={_fmt(block['first_action_correctness'])}; "
            f"divergence={_fmt(block['source_of_truth_divergence'])}; "
            f"wasted reads total={block['wasted_reads_total']}")
    lines.append(f"- runner: {recon['failed']} failures, cause: "
                 + recon['failure_cause'])
    if not judged:
        lines.append('- final correctness cells pending blinded '
                     'evaluation (never zero-filled)')

    lines += ['', '## 11. Interpretation (non-causal)', '',
              ('- Counts and distributions only; single model, single '
               'repository, n per cell as listed. No causal claim about '
               'Asha improving agents is made beyond "on this sample, '
               'condition X differed from condition Y by ...".'),
              ('- Cells with 0 valid trajectories are n/a, never zero; '
               'whether ORIENT or MEM0 help is read only from the '
               'k / N counts in sections 3-7 above.'),
              ('- Verifier numbers reflect the harness-side check '
               'replay at each workspace, not the agent\'s own '
               'confidence.'),
              '']

    lines += ['', '## 12. Limitations', '',
              ('- Missing data: ' + str(recon['failed']) + ' / '
               + str(recon['attempted']) + ' attempted trajectories '
               'invalid (classes: '
               + json.dumps(recon.get('failure_classes') or {},
                            sort_keys=True) + '); '
               + str(recon['excluded'])
               + ' protocol trajectories never attempted (task-001 '
               'excluded at freeze).'),
              ('- Sample size: valid N total = '
               + str(recon['valid'])
               + '; per-cell denominators are in every table above'
               + ('; zero-valid cells ('
                  + ', '.join(zero_conds) + ') are n/a, never zero'
                  if zero_conds else '') + '.'),
              ('- Runner/model dependence: one runner ('
               + str(aggregate_data.get('runner')) + '), one pinned '
               'model '
               f"({aggregate_data['model']}); results are not "
              'runner- or model-general.'),
              ('- Repository dependence: single repository (Asha itself); '
              'task-selection bias toward tasks with replayable ground '
              'truth.'),
              ('- Run variance: distributions reported, means alone '
              'prove nothing.'),
              ('- Timestamp semantics: event_time is harness arrival '
              'time; runner event streams are not treated as an '
              'authoritative cross-event clock, so ms values are '
              'labeled latency and event counts are primary.'),
              ('- Blinding: evaluator packages carry no condition '
              'labels or filenames; behavioral traces inside a diff '
              'could still hint at condition (imperfect blinding).'),
              ('- Agent-side test execution unavailable inside the '
              'sandbox (no network/venv): correctness is judged by the '
              'blinded evaluator plus harness-side verification.'),
              ('- Classification rules (first-action / wasted reads) '
              'are stated annotation rules over ground truth, not '
              'ground truth themselves.'),
              '']
    return '\n'.join(lines)


def cohort_integrity(valid_runs: list[dict],
                     freeze: dict) -> dict[str, Any]:
    """STEP 2/3 verification numbers for section 2 of the report:
    frozen runner/model/commit uniformity plus prompt-semantics audit
    (template byte-equality, GT paths in baseline, condition labels)."""
    tasks = {t['id']: t for t in live_tasks()}
    mismatches = gt_baseline = label_leaks = 0
    for r in valid_runs:
        prompt_path = LIVE / 'runs' / r['run_id'] / 'prompt.txt'
        if not prompt_path.exists():
            mismatches += 1
            continue
        prompt = prompt_path.read_text(encoding='utf-8')
        task = tasks[r['task_id']]
        if prompt != build_prompt(task, r['condition']):
            mismatches += 1
        gt_paths = (task['ground_truth_source']
                    + task['ground_truth_tests'])
        if r['condition'] == 'baseline':
            if any(g in prompt for g in gt_paths):
                gt_baseline += 1
            if 'ASHA ORIENT' in prompt or 'MEMORY' in prompt:
                label_leaks += 1
        if any(x in prompt
               for x in ('orient_mem0', 'condition = baseline')):
            label_leaks += 1
    return {
        'runner_uniform': {r.get('runner')
                           for r in valid_runs} == {freeze.get('runner')},
        'model_uniform': {r.get('model')
                          for r in valid_runs} == {freeze.get('model')},
        'asha_commit_uniform': {
            r.get('asha_commit') for r in valid_runs
        } == {freeze['asha_commit']},
        'prompt_template_mismatches': mismatches,
        'baseline_gt_path_prompts': gt_baseline,
        'condition_label_leaks': label_leaks,
        'valid_per_condition': {
            c: sum(1 for r in valid_runs if r['condition'] == c)
            for c in CONDITIONS},
    }


def cmd_report(args: argparse.Namespace) -> int:
    freeze = read_json(FREEZE)
    runs = [read_json(p) for p in
            sorted((LIVE / 'runs').glob('*/run.json'))]
    if not runs:
        print('LIVE ERROR: no runs', file=sys.stderr)
        return 1
    valid_runs = [r for r in runs if not r.get('invalid')]
    judged = sum(1 for r in valid_runs if r.get('evaluation'))
    aggregate_data = aggregate(runs, freeze)
    aggregate_data['cohort_integrity'] = cohort_integrity(
        valid_runs, freeze)
    aggregate_data['judged_runs'] = judged
    aggregate_data['judged_total'] = len(valid_runs)
    base = LIVE / aggregate_data['run_id']
    base.with_suffix('.json').write_text(
        json.dumps(aggregate_data, indent=2, sort_keys=True),
        encoding='utf-8')
    base.with_suffix('.md').write_text(
        render_report(aggregate_data,
                      judged == len(valid_runs)),
        encoding='utf-8')
    # per-trajectory artifacts (STEP 21 schema lives in each run.json)
    print(f"wrote {base}.json and {base}.md "
          f"(judged {judged}/{len(runs)})")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """STEP 6: staged capacity probe with the FROZEN runner/model in the
    exact benchmark execution mode, before any of the 72 trajectories."""
    freeze = read_json(FREEZE)
    runner = str(freeze.get('runner') or 'codex')
    model = str(freeze.get('model'))
    quota_hits = 0
    results: list[dict] = []
    for i in range(3):
        probe = LIVE / '_preflight' / f'probe_{i}'
        probe.mkdir(parents=True, exist_ok=True)
        ws = probe / 'repo'
        ws.mkdir(exist_ok=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=ws,
                       check=False)
        subprocess.run(['git', 'config', 'user.email', 'p@p'], cwd=ws,
                       check=False)
        subprocess.run(['git', 'config', 'user.name', 'p'], cwd=ws,
                       check=False)
        (ws / 'app.py').write_text('x=1\n', encoding='utf-8')
        subprocess.run(['git', 'add', '-A'], cwd=ws, check=False)
        subprocess.run(['git', 'commit', '-qm', 'init'], cwd=ws,
                       check=False)
        prompt = ('List the files in this repository with `ls`, then '
                  'reply done.')
        if runner == 'opencode':
            cmd = ['opencode', 'run', '-m', model, '--format', 'json',
                   prompt]
        else:
            cmd = ['codex', 'exec', '--json', '--color', 'never',
                   '--ephemeral', '-C', str(ws), '-s', 'workspace-write',
                   '-m', model, '-o', str(probe / 'last.txt'), '-']
        started = time.time()
        try:
            proc = subprocess.run(cmd, cwd=ws, capture_output=True,
                                  text=True, encoding='utf-8',
                                  errors='replace', timeout=300)
            out = proc.stdout
            rc: int | None = proc.returncode
            err = proc.stderr[-500:]
        except subprocess.TimeoutExpired:
            out, rc, err = '', None, 'timeout 300s'
        blob = (out + err).lower()
        hit = any(p in blob for p in QUOTA_PATTERNS + AUTH_PATTERNS)
        quota_hits += int(hit)
        cost = sum(c for c in [
            (json.loads(ln).get('part') or {}).get('cost')
            for ln in out.splitlines() if ln.startswith('{')
            and json.loads(ln).get('type') == 'step_finish']
            if isinstance(c, (int, float)))
        results.append({'probe': i, 'exit_code': rc,
                        'duration_ms': int((time.time() - started) * 1000),
                        'cost_usd': cost, 'quota_or_auth_signal': hit,
                        'stderr_tail': err[-200:]})
        print(f'PREFLIGHT {i}: rc={rc} '
              f'{results[-1]["duration_ms"]}ms cost={cost} '
              f'quota_signal={hit}')
    report = {'runner': runner, 'model': model,
              'probes': results, 'quota_or_auth_signals': quota_hits,
              'capacity_basis': (
                  'no provider quota CLI endpoint; 3 staged probes all '
                  'clean + in-batch stop rule (STEP 11/19)') if
              quota_hits == 0 else 'quota/auth signal seen: STOP before batch'}
    (LIVE / '_preflight.json').write_text(
        json.dumps(report, indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    print(json.dumps({k: report[k] for k in
                      ('runner', 'model', 'quota_or_auth_signals',
                       'capacity_basis')}, indent=2))
    return 0 if quota_hits == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('prepare', help='validate + freeze + payloads + seed')
    sub.add_parser('preflight',
                   help='STEP 6: staged capacity probe before the batch')
    run_parser = sub.add_parser('run', help='execute trajectories A->B->C')
    run_parser.add_argument('--condition', choices=(*CONDITIONS,),
                            default=None)
    score_parser = sub.add_parser('score', help='metrics + verification')
    score_parser.add_argument('--skip-verification', action='store_true')
    sub.add_parser('blind', help='write anonymized judge packages')
    apply_parser = sub.add_parser('apply-judgements',
                                  help='merge blinded verdicts')
    apply_parser.add_argument('--verdicts', required=True)
    sub.add_parser('report', help='aggregate json + human md')
    args = parser.parse_args(argv)
    return {'prepare': cmd_prepare, 'preflight': cmd_preflight,
            'run': cmd_run, 'score': cmd_score, 'blind': cmd_blind,
            'apply-judgements': apply_judgements,
            'report': cmd_report}[args.command](args)


if __name__ == '__main__':
    raise SystemExit(main())


