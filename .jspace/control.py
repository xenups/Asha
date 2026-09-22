#!/usr/bin/env python3
"""J-Space SV1 cooperative host controller with mandatory --transport gate.

Superset of the upstream J-Space controller. Adds a fail-closed transport
gate: every subcommand MUST receive `--transport <ssh|local>`. Omission
prints a diagnostic to stderr and exits 1 BEFORE any state mutation.

Transport is validated (must be exactly `ssh` or `local`) and recorded in
the ledger's top-level `transport` field so every phase of a session can
be audited against a single declared transport mode. No daemons, no
listening ports: pure stdlib file-based ledger.
"""

import argparse
import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '.jspace', 'node_modules', '.venv', 'venv', '__pycache__',
            '.pytest_cache', '.mypy_cache', 'vendor', 'dist', 'build'}
EVENTS = ('tool', 'checkpoint', 'handoff', 'failure', 'resume', 'compact')
TRANSPORTS = ('ssh', 'local')


class ControlError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ControlError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')


def linked(path):
    """Reject reparse points even on Python versions without Path.is_junction."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0)
            & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)))


def file_digest(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def safe_path(root, value, exists=True):
    require(nonempty(str(value)), 'Path must not be empty.')
    candidate = Path(value)
    require(not candidate.is_absolute() and not candidate.drive and '..' not in candidate.parts,
            'Use a relative path inside the task directory; traversal is forbidden.')
    root = root.resolve()
    current = root
    for part in candidate.parts:
        current = current / part
        require(not linked(current),
                'Symbolic links and junctions are forbidden: ' + str(value))
    require(current.resolve().is_relative_to(root), 'Path escapes the task directory.')
    if exists:
        require(current.exists(), 'Missing path: ' + str(value))
    return current


def evidence(root, value):
    path = safe_path(root, value)
    require(path.is_file() and '.jspace' not in {part.casefold() for part in Path(value).parts},
            'Evidence must be a task file outside .jspace (case-insensitive reserved name).')
    before = path.stat()
    fingerprint = hashlib.sha256()
    meaningful = False
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            fingerprint.update(block)
            meaningful = meaningful or bool(block.strip())
    after = path.stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
            'Evidence changed during reading; retry after its writer finishes.')
    require(meaningful, 'Evidence must not be empty.')
    return {'path': path.relative_to(root).as_posix(), 'sha256': fingerprint.hexdigest()}


def current_evidence(root, item, label=None):
    try:
        return isinstance(item, dict) and evidence(root, item['path']) == item
    except (ControlError, OSError) as exc:
        if label:
            raise ControlError(label + ': ' + str(exc)) from exc
        raise


def atomic(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.control-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(root):
    directory = safe_path(root, '.jspace', exists=False)
    directory.mkdir(parents=True, exist_ok=True)
    handle = None
    for attempt in range(50):
        try:
            handle = os.open(str(directory / 'lock'), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode('ascii'))
            break
        except FileExistsError:
            time.sleep(0.05)
    require(handle is not None, 'Control ledger is locked by another process.')
    try:
        root.resolve().stat()
        yield
    finally:
        try:
            os.close(handle)
        finally:
            (directory / 'lock').unlink(missing_ok=True)


def default_state(root, level):
    config = {
        'budget': 64,
        'read_ttl': 3600,
        'level': level,
        'refresh': {'pulse_count': 3, 'pulse_seconds': 240},
        'durations': {'light': 30, 'fast': 60, 'full': 180},
    }
    return {
        'name': 'control',
        'schema': 2,
        'goal': '',
        'next': '',
        'core': [],
        'parked_core': [],
        'level': level,
        'transport': None,
        'config': config,
        'modules': default_modules(level),
        'mod_history': [],
        'checkpoints': [],
        'questions': {},
        'spent': 0,
        'agents': {
            'root': {'parent': None, 'task': '', 'owns': [], 'depth': 0, 'reads': {},
                     'reports': [], 'tools': 0, 'last_pulse': 0, 'view': None,
                     'broadcast': True}
        },
        'solo_reason': '',
        'repo': None,
        'findings': {},
        'tuning': [],
    }


def load(root, state_path=None):
    path = safe_path(root, state_path or '.jspace/control.json')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ControlError('Cannot load control state: ' + str(exc)) from exc


def validate(state):
    require(isinstance(state, dict) and state.get('schema') == 2,
            'Unsupported or missing state schema.')
    for key in ('goal', 'next', 'level', 'transport'):
        if state.get(key) is not None:
            require(nonempty(str(state[key])), key + ' must be a nonempty string.')
    if state.get('transport') is not None:
        require(state['transport'] in TRANSPORTS,
                'transport must be one of ' + ', '.join(TRANSPORTS) +
                '; got ' + repr(state['transport']))
    require(isinstance(state.get('spent', 0), int), 'spent must be an integer.')
    require(isinstance(state.get('agents'), dict) and 'root' in state.get('agents', {}),
            'State must contain a root agent.')
    require(isinstance(state.get('checkpoints', []), list) and
            isinstance(state.get('questions', {}), dict),
            'checkpoints must be a list and questions must be a dict keyed by qid.')
    for checkpoint in state['checkpoints']:
        require(isinstance(checkpoint, dict) and checkpoint.get('id') is not None
                and isinstance(checkpoint.get('claim'), str)
                and isinstance(checkpoint.get('evidence'), dict),
                'Malformed checkpoint: ' + str(checkpoint))
    for qid, question in state['questions'].items():
        require(isinstance(qid, str) and nonempty(qid) and isinstance(question, dict)
                and nonempty(question.get('question'))
                and question.get('checkpoint') is not None
                and question.get('closed') in (True, False),
                'Malformed question ' + str(qid) + ': ' + str(question))


def markdown(state):
    # A view only: never parse Markdown back into authoritative state.
    def quote(value):
        # Keep all task text inside a data block, including headings and newlines.
        return '\n'.join('> ' + line for line in str(value).splitlines()) or '>'

    lines = ['# J-Space shared control state', '', 'Generated from control.json; use the CLI to update.', '',
             '## Goal', quote(state['goal']), '', '## Next', quote(state['next']), '',
             'Level: ' + state['level'], 'Transport: ' + str(state.get('transport')),
             f"Shared credits: {state['spent']} / {state['config']['budget']}", '',
             'Solo limitation:', quote(state['solo_reason']), '', '## Core', *map(quote, state['core']), '',
             '## Parked core', *map(quote, state.get('parked_core', [])), '', '## Verified checkpoints',
             json.dumps(state['checkpoints'], ensure_ascii=False, indent=2), '', '## Questions',
             json.dumps(state['questions'], ensure_ascii=False, indent=2), '', '## Agents']
    for aid, agent in state['agents'].items():
        lines += ['', '### Agent', quote(aid), 'Parent:', quote(agent['parent']), 'Task:', quote(agent['task']),
                  'Owns:', quote(', '.join(agent['owns'])), 'Active: ' + str(agent.get('active', True)),
                  'Retirement: ' + json.dumps(agent.get('retirement'), ensure_ascii=False)]
        for report in agent['reports']:
            lines += [f"Round {report['round']}:", quote(report['summary']),
                      'Evidence: ' + json.dumps(report['evidence'], ensure_ascii=False), 'Next:', quote(report['next']),
                      'Sources: ' + json.dumps(report['sources'], ensure_ascii=False),
                      'Completion: ' + json.dumps(report.get('completion'), ensure_ascii=False),
                      'Review: ' + json.dumps(report.get('review'), ensure_ascii=False)]
    lines += ['', '## Repository map', json.dumps(state.get('repo', {}).get('map') if state.get('repo') else None,
                                                  ensure_ascii=False, indent=2), '', '## Security findings']
    for fid, finding in state['findings'].items():
        lines += ['', '### Finding', quote(fid), json.dumps(finding, ensure_ascii=False, indent=2)]
    return ('\n'.join(lines) + '\n').encode('utf-8')


def save(root, state):
    validate(state)
    state['generation'] += 1
    # Commit canonical JSON first. A crash can leave a stale view, never divergent sources of truth.
    atomic(safe_path(root, '.jspace/control.json', False), encoded(state))
    atomic(safe_path(root, '.jspace/CONTROL.md', False), markdown(state))


def new_agent(parent, task, owns, depth):
    return {'parent': parent, 'task': task, 'owns': owns, 'depth': depth, 'reads': {},
            'reports': [], 'tools': 0, 'last_pulse': 0, 'view': None, 'broadcast': True}


def get_agent(state, aid):
    require(aid in state['agents'], 'Unknown agent: ' + aid)
    require(state['agents'][aid].get('active', True), 'Agent is retired: ' + aid)
    return state['agents'][aid]


def active_agents(state):
    return {aid: agent for aid, agent in state['agents'].items() if agent.get('active', True)}


def spend(state):
    require(state['spent'] < state['config']['budget'], 'Shared controller credit budget exhausted.')
    state['spent'] += 1


def skill_files(state):
    return list(dict.fromkeys(['SKILL.md'] + state['modules']))


def default_modules(level):
    if level == 'xhigh':
        return ['modules/capacity.md', 'modules/orchestration.md']
    if level == 'high':
        return ['modules/capacity.md', 'modules/broadcast.md']
    return ['modules/self-monitoring.md']


def contract_fingerprint(state):
    return digest(encoded({key: state[key] for key in ('goal', 'core', 'level', 'modules')}))


def read_files(state, aid, paths):
    agent = get_agent(state, aid)
    output = []
    for name in paths:
        path = safe_path(SKILL, name)
        require(path.is_file(), 'Skill read requires a file: ' + name)
        data = path.read_bytes()
        output += ['--- BEGIN ' + name + ' SHA256 ' + digest(data) + ' ---', data.decode('utf-8'),
                   '--- END ' + name + ' ---']
        agent['reads'][name] = {'sha256': digest(data), 'time': time.time()}
    required = skill_files(state)
    if (not agent['broadcast'] or set(required).issubset(paths)) and all(name in agent['reads'] and 0 <= time.time() - agent['reads'][name]['time'] <= state['config']['read_ttl']
           and agent['reads'][name]['sha256'] == digest(safe_path(SKILL, name).read_bytes()) for name in required):
        agent['broadcast'] = False
    return '\n'.join(output)


def check_reads(state, aid):
    agent = get_agent(state, aid)
    for name in skill_files(state):
        receipt = agent['reads'].get(name)
        require(receipt is not None, aid + ' must actually read ' + name)
        age = time.time() - receipt['time']
        require(0 <= age <= state['config']['read_ttl'], aid + ' has expired read: ' + name)
        require(digest(safe_path(SKILL, name).read_bytes()) == receipt['sha256'], 'Skill source changed: ' + name)
    require(not agent.get('broadcast'), aid + ' must pulse to receive the pending context broadcast.')


def inventory(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if d.casefold() not in EXCLUDED and not linked(base / d))
        for name in sorted(files):
            path = base / name
            if linked(path):
                continue
            require(path.is_file(), 'Inventory encountered an unsupported filesystem entry.')
            before = path.stat()
            content_hash = file_digest(path)
            after = path.stat()
            require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
                    'Repository changed during inventory; retry after writers finish.')
            result[path.relative_to(root).as_posix()] = content_hash
    return result


def branch(root):
    # Observe branch switches without executing git or following an external worktree pointer.
    git = root / '.git'
    if linked(git):
        return 'linked-git-not-followed'
    head = git / 'HEAD' if git.is_dir() else git
    if head.is_file() and not linked(head):
        return digest(head.read_bytes())
    return None


def check_repo(root, state):
    repo = state['repo']
    require(repo is not None, 'Repository map missing: run repo sync --map PATH, then repo view.')
    require(current_evidence(root, repo['map_evidence']), 'Semantic map source changed; sync it.')
    for index, fact in enumerate(repo['map'].get('facts', []), 1):
        label = 'Map fact ' + str(index) + ' (' + fact['evidence'] + ')'
        require(current_evidence(root, fact['receipt'], label), label + ' evidence changed; update the map and sync it.')
    require(repo['inventory'] == inventory(root) and repo['branch'] == branch(root),
            'Repository changed; update the semantic map and run repo sync.')
    return repo


def map_input(root, filename):
    item = evidence(root, filename)
    value = json.loads(safe_path(root, filename).read_text(encoding='utf-8-sig'))
    require(isinstance(value, dict) and nonempty(value.get('summary')) and isinstance(value.get('areas'), list)
            and bool(value['areas']), 'Map needs summary and a nonempty areas array.')
    for area in value['areas']:
        require(isinstance(area, dict) and nonempty(area.get('path')) and nonempty(area.get('purpose')),
                'Each map area needs path and purpose.')
        safe_path(root, area['path'])
    require(isinstance(value.get('facts', []), list), 'Map facts must be an array.')
    for item_data in value.get('facts', []):
        require(isinstance(item_data, dict) and nonempty(item_data.get('claim')) and
                nonempty(item_data.get('evidence')), 'Map facts need claim and evidence path.')
        item_data['receipt'] = evidence(root, item_data['evidence'])
    return value, item


def repo_enabled(state):
    return state['repo'] is not None or any(name in state['modules'] for name in ('modules/repository.md', 'modules/cyber.md'))


def shared_context(state):
    pending = [aid for aid, a in active_agents(state).items() if a['broadcast']]
    questions = [qid + ': ' + q['question'] for qid, q in state['questions'].items() if not q['closed']]
    reports = [aid + ': ' + a['reports'][-1]['summary'] for aid, a in state['agents'].items() if a['reports']]
    verified = [str(item['id']) + ': ' + item['claim'] + ' [evidence: ' + item['evidence']['path'] + ']'
                for item in state['checkpoints'] if item.get('active', True)]
    return ('Goal: ' + state['goal'] + '\nNext: ' + state['next'] + '\nCore: ' + '; '.join(state['core']) +
            '\nVerified (latest active): ' + '; '.join(verified[-3:]) +
            '\nOpen: ' + '; '.join(questions) + '\nLatest reports: ' + '; '.join(reports) +
            '\nActive agents: ' + ', '.join(active_agents(state)) +
            '\nRetirements: ' + json.dumps({aid: a['retirement'] for aid, a in state['agents'].items()
                                           if not a.get('active', True)}, ensure_ascii=False) +
            '\nPending source broadcasts: ' + ', '.join(pending))


def broadcast_children(state):
    for aid, child in active_agents(state).items():
        if aid != 'root':
            child['broadcast'] = True


def gate(root, state, aid, stage):
    require(nonempty(state['goal']) and nonempty(state['next']), 'Goal and next action are required.')
    require(state.get('transport') in TRANSPORTS,
            'Transport gate: --transport <ssh|local> required before any phase gate.')
    check_reads(state, aid)
    agent = get_agent(state, aid)
    if repo_enabled(state):
        repo = check_repo(root, state)
        require(agent.get('view') == repo['fingerprint'], aid + ' must run repo view before work.')
    for checkpoint in state['checkpoints']:
        if checkpoint.get('active', True):
            require(current_evidence(root, checkpoint['evidence'], 'Active checkpoint ' + str(checkpoint['id'])),
                    'Active checkpoint evidence changed: ' + str(checkpoint['id']) +
                    '. Reopen a dependent question or record a new checkpoint with --supersede ID.')
    for qid, question in state['questions'].items():
        if question['closed']:
            checkpoint = next(
                (item for item in state['checkpoints']
                 if item['id'] == question['checkpoint']), None)
            require(checkpoint is not None,
                    'Closed question ' + str(qid) + ' references missing '
                    'checkpoint ' + str(question['checkpoint']))
            require(current_evidence(root, checkpoint['evidence'], 'Checkpoint ' + str(question['checkpoint'])),
                    'Closed question evidence changed: ' + qid + '. Use note --reopen ' + qid + ' and reverify.')
    if stage == 'ship':
        require(not any(not question['closed'] for question in state['questions'].values()), 'Open questions require evidence-backed closure.')
        require(state['level'] != 'xhigh' or len(active_agents(state)) > 1 or nonempty(state['solo_reason']),
                'xhigh needs participating agents or an explicit --solo-reason describing unavailable host delegation.')
        for member, details in active_agents(state).items():
            check_reads(state, member)
            if repo_enabled(state):
                require(details.get('view') == repo['fingerprint'], member + ' must run repo view before shipment.')


def apply_transport(state, transport):
    """Fail-closed gate: record the declared transport in the ledger."""
    require(transport in TRANSPORTS,
            'Transport gate: --transport must be one of <ssh|local>; got ' + repr(transport))
    state['transport'] = transport


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(description='J-Space cooperative controller (fail-closed transport gate).')
    ap.add_argument('--transport', dest='transport', default=None,
                    help='MANDATORY execution transport: ssh or local. Omission exits 1.')
    ap.add_argument('--root', default='.', help='task root (default: current directory)')
    ap.add_argument('--no-lock', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--help-internal', action='store_true', help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest='command', required=True)

    def args_goal(parser, has_level=True):
        parser.add_argument('--goal', required=True)
        parser.add_argument('--next', required=True)
        parser.add_argument('--core', action='append', default=[])
        parser.add_argument('--agent', default='root')
        if has_level:
            parser.add_argument('--level', default='medium',
                                choices=['low', 'medium', 'media', 'high', 'xhigh'])

    p = sub.add_parser('init')
    args_goal(p)
    p.add_argument('--solo-reason', default='', dest='solo_reason')

    p = sub.add_parser('read')
    p.add_argument('--agent', default='root')
    p.add_argument('targets', nargs='*')

    p = sub.add_parser('pulse')
    p.add_argument('--event', required=True, choices=EVENTS)
    p.add_argument('--agent', default='root')
    p.add_argument('--count', type=int, default=None)
    p.add_argument('--label', default='')

    p = sub.add_parser('orient')
    p.add_argument('--mode', default='standard',
                   choices=['quick', 'standard', 'deep'])
    p.add_argument('--format', dest='fmt', default='json',
                   choices=['json', 'markdown'])
    p.add_argument('--no-cache', action='store_true')

    p = sub.add_parser('check')
    p.add_argument('--stage', required=True, choices=['work', 'ship'])

    p = sub.add_parser('checkpoint')
    p.add_argument('--claim', required=True)
    p.add_argument('--evidence', required=True)
    p.add_argument('--question', action='append', default=[])
    p.add_argument('--supersede', type=int, default=None)

    p = sub.add_parser('question')
    p.add_argument('--open', dest='open_q', metavar='TEXT', default=None)
    p.add_argument('--checkpoint', type=int, default=1)
    p.add_argument('--reopen', type=int, default=None)
    p.add_argument('--close', type=int, default=None)
    p.add_argument('--evidence', default=None)

    p = sub.add_parser('note')
    p.add_argument('--add', dest='add_note', metavar='CLAIM', default=None)
    p.add_argument('--park', dest='park_core', metavar='CLAIM', default=None)
    p.add_argument('--next', dest='next_action', default=None)
    p.add_argument('--agent', default='root')

    p = sub.add_parser('route')
    p.add_argument('--module', action='append', required=True)
    p.add_argument('--reason', required=True)
    p.add_argument('--level', default=None, choices=['low', 'medium', 'media', 'high', 'xhigh'])

    p = sub.add_parser('tune')
    p.add_argument('--pulse-count', type=int)
    p.add_argument('--pulse-seconds', type=int)
    p.add_argument('--reason', required=True)

    p = sub.add_parser('report')
    p.add_argument('--summary', required=True)
    p.add_argument('--evidence', action='append', default=[])
    p.add_argument('--next', required=True)
    p.add_argument('--sources', action='append', default=[])
    p.add_argument('--complete', default=None)
    p.add_argument('--review', default=None)

    p = sub.add_parser('retire')
    p.add_argument('--agent', required=True)
    p.add_argument('--reason', required=True)

    p = sub.add_parser('spawn')
    p.add_argument('--agent', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--owns', action='append', default=[])
    p.add_argument('--parent', default='root')

    p = sub.add_parser('repo')
    repo_sub = p.add_subparsers(dest='repo_command', required=True)
    ps = repo_sub.add_parser('sync')
    ps.add_argument('--map', required=True)
    ps.add_argument('--summary', required=True)
    pv = repo_sub.add_parser('view')
    pv.add_argument('--agent', default='root')
    pf = repo_sub.add_parser('findings')
    pf.add_argument('--save', default=None)
    pf.add_argument('--dismiss', action='append', default=[])

    p = sub.add_parser('finding')
    p.add_argument('--add', dest='add_finding', metavar='TITLE', required=True)
    p.add_argument('--evidence', action='append', required=True)
    p.add_argument('--threat', required=True)
    p.add_argument('--mitigation', required=True)

    p = sub.add_parser('audit')
    p.add_argument('--full', action='store_true')

    p = sub.add_parser('status')
    p.add_argument('--json', action='store_true')

    ns = ap.parse_args(argv)

    # --- FAIL-CLOSED TRANSPORT GATE ------------------------------------
    if ns.transport is None:
        print('TRANSPORT GATE: --transport <ssh|local> is MANDATORY. Omission is a hard failure.',
              file=sys.stderr)
        print('Usage: control.py --transport <ssh|local> ' + ns.command + ' ...', file=sys.stderr)
        sys.exit(1)
    if ns.transport not in TRANSPORTS:
        print('TRANSPORT GATE: --transport must be one of <ssh|local>; got ' + repr(ns.transport),
              file=sys.stderr)
        sys.exit(1)

    root = Path(ns.root).resolve()
    ctx = locked(root) if not ns.no_lock else contextlib.nullcontext()

    with ctx:
        if ns.command == 'orient':
            # Ledger-free read-only perception: delegates to project_map.py
            # (facts + provenance). Transport still declared; no state write.
            script = SKILL / '.hermes' / 'tools' / 'project_map.py'
            argv = [sys.executable, str(script), '--' + ns.mode,
                    '--format', ns.fmt, '--root', str(root)]
            if ns.no_cache:
                argv.append('--no-cache')
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=300)
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            if proc.returncode != 0:
                sys.exit(proc.returncode or 1)
            return 0
        if ns.command == 'init':
            state = default_state(root, 'medium' if ns.level in ('media', 'medium') else ns.level)
            state['goal'] = ns.goal
            state['next'] = ns.next
            state['core'] = ns.core
            state['solo_reason'] = ns.solo_reason
            apply_transport(state, ns.transport)
            state['generation'] = 0
            save(root, state)
            print('init: ' + ns.goal)
            print('transport: ' + ns.transport)
        else:
            state = load(root)
            try:
                validate(state)
            except ControlError as exc:
                print('Control state invalid: ' + str(exc), file=sys.stderr)
                sys.exit(1)
            # Persist transport from CLI onto the ledger (idempotent if same).
            if state.get('transport') is None:
                apply_transport(state, ns.transport)
            require(state['transport'] == ns.transport,
                    'Transport mismatch: ledger has ' + repr(state['transport']) +
                    ' but CLI passed ' + repr(ns.transport) + '. Refusing to mix transports.')

        if ns.command == 'init':
            pass
        elif ns.command == 'read':
            print(read_files(state, ns.agent, ns.targets or ['SKILL.md']))
            # Receipts must be durable: check_reads verifies them in later
            # processes, so an unsaved read would make every gate fail-closed
            # forever (regression: dict/list questions aside, this made the
            # ship gate unreachable across CLI invocations).
            save(root, state)
        elif ns.command == 'pulse':
            agent = get_agent(state, ns.agent)
            agent['tools'] += 1
            agent['last_pulse'] = time.time()
            event = getattr(ns, 'event', 'tool')
            if event == 'tool':
                label = ns.label or 'tool execution'
                print('tool pulse: ' + label)
            else:
                print('event: ' + event + (': ' + ns.label if ns.label else ''))
            if ns.count is not None or ns.agent != 'root':
                state['config']['refresh'] = {'pulse_count': ns.count or state['config']['refresh']['pulse_count'],
                                              'pulse_seconds': state['config']['refresh']['pulse_seconds']}
            save(root, state)
        elif ns.command == 'check':
            gate(root, state, ns.agent if hasattr(ns, 'agent') else 'root', ns.stage)
            if ns.stage == 'ship':
                # Scoped evidence gate: delegate to the toolchain modules.
                tools_dir = SKILL / '.hermes' / 'tools'
                if tools_dir.is_dir() and str(tools_dir) not in sys.path:
                    sys.path.insert(0, str(tools_dir))
                import check_runner
                import evidence as evidence_engine
                import scope_resolver
                try:
                    # Tamper check on any prior artifact BEFORE anything else.
                    evidence_engine.verify(root)
                    evidence_engine.require_clean_tree(root)
                    resolved = scope_resolver.resolve(root)
                    checks = check_runner.run(root, resolved)
                    ok = all(c['status'] in ('passed', 'skipped')
                             for c in checks)
                    sealed = evidence_engine.seal({
                        'schema': evidence_engine.SCHEMA,
                        'stage': 'ship',
                        'scope': resolved['scope'],
                        'commit': evidence_engine.head_hash(root),
                        'tree_hash': evidence_engine.tree_hash(root),
                        'observed_at': evidence_engine.now_iso(),
                        'checks': checks,
                        'authorized_to_ship': ok,
                    })
                    evidence_engine.write(root, sealed)
                    evidence_engine.verify(root)  # roundtrip self-check
                except (evidence_engine.EvidenceError,
                        scope_resolver.ScopeError,
                        check_runner.CheckRunnerError) as exc:
                    print('SHIP GATE REFUSED: ' + str(exc), file=sys.stderr)
                    sys.exit(1)
                print('scope: ' + resolved['scope'] +
                      ' (' + resolved['status'] + ')')
                print('evidence: .jspace/evidence.json')
                if not ok:
                    failed = ', '.join(c['name'] for c in checks
                                       if c['status'] == 'failed')
                    print('GATE SHIP: FAIL -- checks failed: ' + failed,
                          file=sys.stderr)
                    sys.exit(1)
            print('GATE ' + ns.stage.upper() + ': PASS')
        elif ns.command == 'checkpoint':
            receipt = evidence(root, ns.evidence)
            cid = len(state['checkpoints']) + 1
            entry = {'id': cid, 'claim': ns.claim, 'evidence': receipt, 'active': True,
                     'supersedes': ns.supersede}
            if ns.supersede:
                require(1 <= ns.supersede < cid, 'Supersede target must be an earlier checkpoint id.')
                for cp in state['checkpoints']:
                    if cp['id'] == ns.supersede:
                        cp['active'] = False
            state['checkpoints'].append(entry)
            for text in ns.question:
                qid = str(len(state['questions']) + 1)
                state['questions'][qid] = {'question': text, 'checkpoint': cid, 'closed': False}
            save(root, state)
            print('checkpoint ' + str(cid) + ': ' + ns.claim)
            for text in ns.question:
                print('  open question: ' + text)
        elif ns.command == 'question':
            if ns.open_q:
                qid = str(len(state['questions']) + 1)
                state['questions'][qid] = {'question': ns.open_q, 'checkpoint': ns.checkpoint,
                                           'closed': False}
                save(root, state)
                print('question ' + qid + ' open: ' + ns.open_q)
            elif ns.reopen:
                q = next((q for existing, q in state['questions'].items()
                          if existing == str(ns.reopen)
                          or q['checkpoint'] == ns.reopen), None)
                require(q is not None, 'No question with that id.')
                q['closed'] = False
                save(root, state)
                print('question reopened: ' + str(ns.reopen))
            elif ns.close:
                q = next((q for q in state['questions'].values()
                          if q['checkpoint'] == ns.close), None)
                require(q is not None, 'No open question for checkpoint ' + str(ns.close))
                require(ns.evidence, 'closing a question requires --evidence')
                receipt = evidence(root, ns.evidence)
                q['closed'] = True
                q['evidence'] = receipt
                save(root, state)
                print('question closed with evidence: ' + receipt['path'])
        elif ns.command == 'note':
            if ns.add_note:
                state['core'].append(ns.add_note)
            if ns.park_core:
                state['parked_core'].append(ns.park_core)
                state['core'] = [c for c in state['core'] if c != ns.park_core]
            if ns.next_action:
                state['next'] = ns.next_action
            save(root, state)
            print('note recorded' + (': ' + ns.add_note if ns.add_note else ''))
            print('next: ' + state['next'])
        elif ns.command == 'route':
            state['mod_history'].append({'modules': list(state['modules']), 'reason': ns.reason})
            state['modules'] = list(dict.fromkeys(ns.module))
            if ns.level:
                state['level'] = ns.level
            save(root, state)
            print('routed to: ' + ', '.join(state['modules']))
        elif ns.command == 'tune':
            old = dict(state['config']['refresh'])
            if ns.pulse_count:
                state['config']['refresh']['pulse_count'] = ns.pulse_count
            if ns.pulse_seconds:
                state['config']['refresh']['pulse_seconds'] = ns.pulse_seconds
            state['tuning'].append({'from': old, 'to': dict(state['config']['refresh']),
                                    'reason': ns.reason})
            save(root, state)
            print('tuned: ' + str(state['config']['refresh']))
        elif ns.command == 'report':
            agent = get_agent(state, ns.agent)
            round_no = len(agent['reports']) + 1
            report = {'round': round_no, 'summary': ns.summary, 'next': ns.next,
                      'sources': ns.sources, 'completion': ns.complete, 'review': ns.review}
            report['evidence'] = [evidence(root, e) for e in ns.evidence]
            agent['reports'].append(report)
            state['next'] = ns.next
            save(root, state)
            print('report ' + ns.agent + ' round ' + str(round_no))
        elif ns.command == 'retire':
            agent = get_agent(state, ns.agent)
            agent['active'] = False
            agent['retirement'] = {'reason': ns.reason, 'time': time.time()}
            save(root, state)
            print('retired: ' + ns.agent)
        elif ns.command == 'spawn':
            require(ns.agent != 'root', 'root is the coordinator; spawn named agents only.')
            require(ns.agent not in state['agents'], 'Agent already exists: ' + ns.agent)
            parent = get_agent(state, ns.parent)
            state['agents'][ns.agent] = new_agent(ns.parent, ns.task, ns.owns, parent['depth'] + 1)
            save(root, state)
            print('spawned: ' + ns.agent)
        elif ns.command == 'repo':
            if ns.repo_command == 'sync':
                value, item = map_input(root, ns.map)
                inventory_now = inventory(root)
                state['repo'] = {'map': value, 'map_evidence': item,
                                 'inventory': inventory_now,
                                 'branch': branch(root),
                                 'fingerprint': digest(encoded({'map': value, 'inventory': inventory_now}))}
                save(root, state)
                print('repo synced: ' + ns.summary)
            elif ns.repo_command == 'view':
                repo = check_repo(root, state)
                state['agents'][ns.agent]['view'] = repo['fingerprint']
                save(root, state)
                print('repo view confirmed: ' + repo['map']['summary'])
            elif ns.repo_command == 'findings':
                if ns.save:
                    findings_path = safe_path(root, ns.save, exists=False)
                    findings_path.write_text(json.dumps(state['findings'], ensure_ascii=False, indent=2),
                                             encoding='utf-8')
                    print('findings saved to ' + ns.save)
                for fid in ns.dismiss:
                    if fid in state['findings']:
                        del state['findings'][fid]
                        print('finding dismissed: ' + fid)
                if ns.save or ns.dismiss:
                    save(root, state)
        elif ns.command == 'finding':
            fid = 'F' + str(len(state['findings']) + 1)
            state['findings'][fid] = {'title': ns.add_finding, 'threat': ns.threat,
                                      'mitigation': ns.mitigation,
                                      'evidence': [evidence(root, e) for e in ns.evidence]}
            save(root, state)
            print('finding ' + fid + ': ' + ns.add_finding)
        elif ns.command == 'audit':
            gate(root, state, 'root', 'work')
            print('audit: control state is consistent and transport-locked: ' + str(state['transport']))
        elif ns.command == 'status':
            if ns.json:
                print(json.dumps(state, ensure_ascii=False, indent=2))
            else:
                print('goal: ' + state['goal'])
                print('next: ' + state['next'])
                print('level: ' + state['level'])
                print('transport: ' + str(state['transport']))
                print('checkpoints: ' + str(len(state['checkpoints'])))
                print('open questions: ' + str(sum(1 for q in state['questions'].values()
                                                  if not q['closed'])))
                print('agents: ' + ', '.join(active_agents(state)))
                print('spent: ' + str(state['spent']) + '/' + str(state['config']['budget']))

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except ControlError as exc:
        print('CONTROL ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)