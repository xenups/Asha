"""Phase 4.3 -- MCP integration, telemetry and fail-closed routing tests.

Covers, in-process via the existing ``handle_message`` (no MCP
subprocess): env-only Fast Path configuration, the two new tools
(``asha_get_surgical_context``, ``asha_dispatch_task``), the
recorded-execution classifier envelope adapter, the FULL/FAST routing
invariants on real dispatches, and JSONL telemetry privacy + fail-safe
semantics (telemetry failure never changes authorization).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import asha.mcp_server as server
from asha import evidence
from asha.common import paths as common_paths


def _cache_dir(repo: Path, task_id: str = 'seed-task') -> Path:
    """External orchestrator worker-evidence dir (Phase D zone)."""
    return common_paths.get_orchestrator_dir(repo) / task_id


def _telemetry(repo: Path) -> Path:
    """External MCP live telemetry file (Phase D zone)."""
    return common_paths.get_state_dir(repo) / 'mcp_live_telemetry.jsonl'


PY = sys.executable
_BASELINE = {
    '.gitignore': ('.jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n'
                   '.mypy_cache/\n.ruff_cache/\n'),
    'README.md': '# fixture\n',
    'pyproject.toml': '[tool.ruff]\nline-length = 88\n',
    'tests/test_ok.py': 'def test_ok():\n    assert True\n',
    'pkg/__init__.py': '',
    'pkg/base.py': ('VALUE = "base"\n'
                    '\n'
                    'def base_helper(x: int) -> int:\n'
                    '    return x + 1\n'),
    'pkg/user.py': ('from pkg.base import base_helper\n'
                    '\n'
                    'def use_base(y: int) -> int:\n'
                    '    return base_helper(y)\n'),
    'pkg/dynamic.py': ('NAME = "pkg.base"\n'
                       'mod = __import__(NAME)\n'),
    'pkg/uses_ghost.py': ('from ghost_lib import thing\n'
                          '\n'
                          'def go():\n'
                          '    return thing()\n'),
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 'fixture@example.com')
    _git(repo, 'config', 'user.name', 'Fixture')
    _git(repo, 'config', 'commit.gpgsign', 'false')
    for rel, text in _BASELINE.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'baseline')
    return repo


def _call(tool: str, arguments: dict[str, Any],
          request_id: int = 1) -> dict[str, Any]:
    response = server.handle_message({
        'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
        'params': {'name': tool, 'arguments': arguments}})
    assert response is not None
    if 'error' in response:
        return {'_transport_error': response['error']}
    result = response['result']
    text = result['content'][0]['text']
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {'_text': text}
    return {'isError': result['isError'], 'payload': payload}


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert not result.get('isError'), result
    assert '_transport_error' not in result, result
    payload = result['payload']
    assert isinstance(payload, dict), result
    return payload


def _seed_record(repo: Path, worker_id: str,
                 reads: list[str] | None, writes: list[str] | None,
                 *, name: str | None = None,
                 tamper: bool = False) -> Path:
    """A real sealed worker-evidence record (the envelope's input
    contract); `tamper=True` flips a field after sealing."""
    payload = evidence.seal({
        'schema': evidence.SCHEMA, 'stage': 'worker', 'scope': 'S2',
        'commit': '0' * 40, 'tree_hash': '1' * 40,
        'observed_at': evidence.now_iso(), 'checks': [],
        'authorized_to_ship': False, 'task_id': 'seed',
        'worker_id': worker_id, 'base_commit': '0' * 40,
        'base_tree_sha': '1' * 40, 'target_tree_sha': '2' * 40,
        'declared_scope': list(writes or reads or []),
        'observed_scope': [], 'read_set': reads, 'write_set': writes,
        'diff': '', 'exit_status': 0,
    })
    if tamper:
        payload = dict(payload)
        payload['write_set'] = ['tampered.py']
    # real scheduler layout: evidence_dir = cache/<orchestrator>/<task_id>
    cache = _cache_dir(repo, 'seed-task')
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (name or f'{worker_id}.json')
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def _dispatch(repo: Path, wid: str, *, writes: list[str],
              reads: list[str] | None = None,
              extra: dict[str, Any] | None = None,
              declared: list[str] | None = None) -> dict[str, Any]:
    marker = (f'from pathlib import Path; '
              f'Path({writes[0]!r}).write_text("# probe\\n")')
    arguments: dict[str, Any] = {
        'id': wid,
        'declared_scope': declared if declared is not None else list(writes),
        'reads': reads if reads is not None else list(writes),
        'writes': list(writes),
        'deps': [], 'cmd': [PY, '-c', marker],
        'root': str(repo),
    }
    if extra:
        arguments.update(extra)
    return _call('asha_dispatch_task', arguments)


def _events(repo: Path) -> list[dict[str, Any]]:
    path = _telemetry(repo)
    if not path.is_file():
        return []
    lines = path.read_text(encoding='utf-8').splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------
# 1. Fast Path runtime configuration (env-only)
# ---------------------------------------------------------------------
def test_env_flag_exact_one_only(monkeypatch) -> None:
    for value, expected in ((None, False), ('1', True), ('true', False),
                            ('0', False), ('1 ', False), ('2', False),
                            ('', False), ('yes', False)):
        if value is None:
            monkeypatch.delenv(server.FAST_PATH_ENV, raising=False)
        else:
            monkeypatch.setenv(server.FAST_PATH_ENV, value)
        assert server._fast_path_enabled() is expected, value


def test_no_input_schema_carries_fast_path() -> None:
    for spec in server.TOOL_SPECS:
        schema = json.dumps(spec['inputSchema'])
        assert 'fast_path_enabled' not in schema, spec['name']
    for name in ('asha_dispatch_task', 'asha_get_surgical_context'):
        spec = next(s for s in server.TOOL_SPECS if s['name'] == name)
        assert spec['inputSchema']['additionalProperties'] is False


def test_client_cannot_supply_runtime_config(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    result = _dispatch(repo, 'c1', writes=['probe_c1.py'],
                       extra={'fast_path_enabled': True})
    assert result.get('isError') is True
    assert 'unknown field' in str(result['payload'])
    # nothing executed, nothing recorded
    cache = _cache_dir(repo)
    assert not cache.exists() or list(cache.rglob('*.json')) == []


# ---------------------------------------------------------------------
# 2. asha_get_surgical_context (Phase 4.1 pipeline)
# ---------------------------------------------------------------------
def test_surgical_context_pipeline_success(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    result = _call('asha_get_surgical_context',
                   {'target_file': 'pkg/user.py',
                    'target_symbol': 'use_base',
                    'repo_path': str(repo)})
    payload = _payload(result)
    assert payload['target'] == 'pkg/user.py:use_base'
    assert set(payload) == {'target', 'context', 'dependencies',
                            'unresolved', 'full_source_bytes',
                            'context_source_bytes', 'reduction_ratio'}
    # cross-module dependency resolved through the real pipeline
    assert 'sym:pkg.base:base_helper' in payload['dependencies']
    assert 'def use_base' in payload['context']
    assert 'def base_helper' in payload['context']  # inlined stub
    # byte accounting: reduction_ratio from SOURCE BYTES only
    assert payload['context_source_bytes'] == len(
        payload['context'].encode('utf-8'))
    assert payload['full_source_bytes'] >= payload['context_source_bytes']
    assert 0.0 <= payload['reduction_ratio'] <= 1.0
    assert isinstance(payload['unresolved'], list)


def test_surgical_context_no_silent_dependency_drop(tmp_path) -> None:
    """Independent recomputation: payload.dependencies must equal the
    closure the pipeline computes -- nothing dropped, dynamic-import
    boundary included."""
    from asha.ast_indexer import index_module
    from asha.codegraph import build_graph, closure, sym_node

    repo = _make_repo(tmp_path)
    result = _call('asha_get_surgical_context',
                   {'target_file': 'pkg/dynamic.py',
                    'target_symbol': 'NAME',
                    'repo_path': str(repo)})
    payload = _payload(result)
    indices = []
    for rel, module in (('pkg/dynamic.py', 'pkg.dynamic'),
                        ('pkg/base.py', 'pkg.base')):
        indices.append(index_module(
            module, (repo / rel).read_text(encoding='utf-8')))
    graph = build_graph(tuple(indices))
    root = sym_node('pkg.dynamic', 'NAME')
    reach = closure(graph, (root,), 'pkg.dynamic')
    assert sorted(payload['dependencies']) == sorted(
        node for node in reach.reachable if node != root)
    assert sorted(payload['unresolved']) == sorted(reach.unresolved)
    # an unresolvable import surfaces as a reported boundary node,
    # still present in dependencies (nothing dropped) + visible text
    ghost = _call('asha_get_surgical_context',
                  {'target_file': 'pkg/uses_ghost.py',
                   'target_symbol': 'go',
                   'repo_path': str(repo)})
    ghost_payload = _payload(ghost)
    assert any(node.startswith(('ext:', 'unk:'))
               for node in ghost_payload['dependencies']), ghost_payload
    assert 'external dependency' in ghost_payload['context'] or \
        'UNRESOLVED' in ghost_payload['context']


@pytest.mark.parametrize('arguments,fragment', [
    ({'target_symbol': 'x', 'repo_path': '.'}, 'target_file'),
    ({'target_file': 'a.py', 'repo_path': '.'}, 'target_symbol'),
    ({'target_file': ['a.py'], 'target_symbol': 'x'}, 'target_file'),
    ({'target_file': '../escape.py', 'target_symbol': 'x'}, 'inside'),
    ({'target_file': '/abs/path.py', 'target_symbol': 'x'}, 'inside'),
    ({'target_file': 'README.md', 'target_symbol': 'x'}, '.py'),
    ({'target_file': 'missing.py', 'target_symbol': 'x'}, 'not found'),
    ({'target_file': 'a.py', 'target_symbol': 'x',
      'repo_path': '/nonexistent-dir-asha'}, 'not exist'),
    ({'target_file': 'a.py', 'target_symbol': 'x',
      'repo_path': '.', 'extra': 1}, 'unknown field'),
])
def test_surgical_context_fails_closed(
        arguments: dict[str, Any], fragment: str) -> None:
    result = _call('asha_get_surgical_context', arguments)
    assert result.get('isError') is True, result
    assert fragment in str(result['payload']).lower() or \
        fragment in str(result['payload'])


def test_surgical_context_symbol_not_in_file(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    result = _call('asha_get_surgical_context',
                   {'target_file': 'pkg/user.py',
                    'target_symbol': 'does_not_exist',
                    'repo_path': str(repo)})
    assert result.get('isError') is True
    assert 'symbol not found' in str(result['payload'])


# ---------------------------------------------------------------------
# 3. asha_dispatch_task -- fail-closed inputs, UNKNOWN / SHARED / DISJOINT
# ---------------------------------------------------------------------
def test_dispatch_invalid_inputs_structured_error(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    cases: list[dict[str, Any]] = [
        {'declared_scope': ['x.py'], 'cmd': [PY, '-c', 'pass']},
        {'id': 'w', 'cmd': [PY, '-c', 'pass']},
        {'id': 'w', 'declared_scope': ['x.py']},
        {'id': 'w', 'declared_scope': 'x.py', 'cmd': [PY, '-c', 'pass']},
        {'id': 'w', 'declared_scope': ['x.py'], 'cmd': 'str'},
        {'id': 'w', 'declared_scope': ['x.py'], 'cmd': [],
         'root': str(repo)},
        {'id': 'w', 'declared_scope': ['x.py'], 'cmd': ['x', 1]},
        {'id': 'w', 'declared_scope': ['x.py'], 'cmd': ['x'],
         'root': '/nonexistent-root-asha'},
        {'id': 'w', 'declared_scope': ['x.py'], 'cmd': ['x'],
         'reads': 'not-a-list'},
    ]
    for arguments in cases:
        result = _call('asha_dispatch_task', arguments)
        assert result.get('isError') is True, arguments
    # no execution side effects from any rejected call
    cache = _cache_dir(repo)
    assert not cache.exists() or list(cache.rglob('*.json')) == []


def test_dispatch_unknown_never_fast(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(server.FAST_PATH_ENV, '1')  # even with flag on
    repo = _make_repo(tmp_path)
    result = _dispatch(repo, 'u1', writes=['probe_u1.py'])
    payload = _payload(result)
    assert payload['classification'] == 'UNKNOWN'
    assert payload['runtime_mode'] == 'full_governance'
    assert payload['reason_code'] == 'missing_context'  # no records yet
    # full governance actually ran
    assert payload['worktrees'], payload
    assert payload['evidence']
    assert payload['state'] == 'DONE'
    assert payload['authorized_to_ship'] is False


def test_dispatch_shared_never_fast(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(server.FAST_PATH_ENV, '1')
    repo = _make_repo(tmp_path)
    _seed_record(repo, 'prior', reads=['shared_x.py'],
                 writes=['shared_x.py'])
    result = _dispatch(repo, 's1', writes=['shared_x.py'],
                       reads=['shared_x.py'])
    payload = _payload(result)
    assert payload['classification'] == 'PROVEN_SHARED'
    assert payload['runtime_mode'] == 'full_governance'
    assert payload['worktrees']
    assert payload['state'] == 'DONE'


def test_dispatch_disjoint_flag_off_full(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(server.FAST_PATH_ENV, raising=False)
    repo = _make_repo(tmp_path)
    _seed_record(repo, 'prior', reads=['other.py'], writes=['other.py'])
    result = _dispatch(repo, 'd1', writes=['probe_d1.py'])
    payload = _payload(result)
    assert payload['classification'] == 'PROVEN_DISJOINT'
    assert payload['runtime_mode'] == 'full_governance'
    assert payload['routing_reason'] == 'flag_disabled'
    assert payload['fast_path_enabled'] is False
    assert payload['worktrees'], payload
    assert payload['state'] == 'DONE'


def test_dispatch_disjoint_flag_on_fast(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(server.FAST_PATH_ENV, '1')
    repo = _make_repo(tmp_path)
    _seed_record(repo, 'prior', reads=['other.py'], writes=['other.py'])
    result = _dispatch(repo, 'd2', writes=['probe_d2.py'])
    payload = _payload(result)
    assert payload['classification'] == 'PROVEN_DISJOINT'
    assert payload['runtime_mode'] == 'fast_path'
    assert payload['fast_path_enabled'] is True
    # actual fast behavior: no isolated worktree was created
    assert payload['worktrees'] == {}, payload
    # routing instrumentation agrees with the pre-dispatch decision
    routing = payload['routing']
    assert routing['d2']['mode'] == 'fast_path'
    assert routing['d2']['classification'] == 'PROVEN_DISJOINT'
    # evidence contract intact on the fast path
    assert payload['evidence']
    evidence_path = Path(payload['evidence']['d2'])
    assert evidence_path.is_file()
    sealed = json.loads(evidence_path.read_text(encoding='utf-8'))
    assert sealed['authorized_to_ship'] is False
    assert sealed['evidence_sha256'] == evidence.compute_digest(sealed)
    assert payload['state'] == 'DONE'


def test_dispatch_tampered_record_fails_closed(tmp_path,
                                               monkeypatch) -> None:
    monkeypatch.setenv(server.FAST_PATH_ENV, '1')
    repo = _make_repo(tmp_path)
    _seed_record(repo, 'prior', reads=['other.py'], writes=['other.py'],
                 tamper=True)
    result = _dispatch(repo, 't1', writes=['probe_t1.py'])
    payload = _payload(result)
    assert payload['classification'] == 'UNKNOWN'
    assert payload['runtime_mode'] == 'full_governance'


def test_dispatch_classifier_exception_no_execution(tmp_path,
                                                    monkeypatch) -> None:
    repo = _make_repo(tmp_path)

    def boom(task, context=()):
        raise RuntimeError('classifier exploded')

    monkeypatch.setattr(server, 'classify_task', boom)
    result = _dispatch(repo, 'x1', writes=['probe_x1.py'])
    assert result.get('isError') is True
    assert 'fail-closed' in str(result['payload'])
    assert 'nothing executed' in str(result['payload'])
    cache = _cache_dir(repo)
    assert not cache.exists() or list(cache.rglob('*.json')) == []


def test_dispatch_result_contract(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(server.FAST_PATH_ENV, raising=False)
    repo = _make_repo(tmp_path)
    payload = _payload(_dispatch(repo, 'k1', writes=['probe_k1.py']))
    assert set(payload) >= {'task_id', 'state', 'classification',
                            'reason_code', 'runtime_mode',
                            'routing_reason', 'routing', 'evidence',
                            'worktrees', 'timings', 'authorized_to_ship'}
    assert payload['task_id'].startswith('mcp-')
    assert payload['authorized_to_ship'] is False
    timings = payload['timings']
    assert set(timings) == {'classification_ms', 'routing_ms',
                            'context_ms', 'scheduler_ms', 'execution_ms',
                            'evidence_ms', 'total_ms'}
    for value in timings.values():
        assert isinstance(value, (int, float)) and value >= 0.0


def test_dispatch_classifies_against_real_recorded_envelope(
        tmp_path, monkeypatch) -> None:
    """The real->real chain (regression caught by Phase 4.3 dogfooding):
    a dispatch's SECOND task must classify against the evidence the
    scheduler ACTUALLY sealed for the first one -- at the real
    evidence_dir layout (cache/orchestrator/<task_id>/<wid>.json) --
    not only against hand-seeded files."""
    monkeypatch.delenv(server.FAST_PATH_ENV, raising=False)
    repo = _make_repo(tmp_path)
    first = _payload(_dispatch(repo, 'rr1', writes=['probe_rr1.py']))
    assert first['classification'] == 'UNKNOWN'  # no records yet
    second = _payload(_dispatch(repo, 'rr2', writes=['probe_rr2.py']))
    # discovered real record of rr1 -> proven disjoint against it
    assert second['classification'] == 'PROVEN_DISJOINT'
    assert second['runtime_mode'] == 'full_governance'
    third = _dispatch(repo, 'rr3', writes=['probe_rr1.py'],
                      reads=['probe_rr1.py'])
    payload = _payload(third)                    # real overlap with rr1
    assert payload['classification'] == 'PROVEN_SHARED'
    assert payload['runtime_mode'] == 'full_governance'


# ---------------------------------------------------------------------
# 4. Telemetry (JSONL, privacy, fail-safe)
# ---------------------------------------------------------------------
def test_telemetry_success_and_failure_events(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    ok = _call('asha_get_surgical_context',
               {'target_file': 'pkg/user.py', 'target_symbol': 'use_base',
                'repo_path': str(repo)})
    assert not ok['isError']
    bad = _call('asha_get_surgical_context',
                {'target_file': 'missing.py', 'target_symbol': 'x',
                 'repo_path': str(repo)})
    assert bad['isError']
    events = _events(repo)
    assert len(events) == 2, events
    for event in events:
        assert set(event) == {'timestamp', 'tool', 'request_id',
                              'duration_ms', 'status', 'metadata'}
        assert event['tool'] == 'asha_get_surgical_context'
        assert isinstance(event['duration_ms'], float)
        assert event['duration_ms'] >= 0.0
        assert event['status'] in ('ok', 'error', 'unknown_tool')
    assert events[0]['status'] == 'ok'
    assert events[0]['metadata']['target'] == 'pkg/user.py:use_base'
    assert events[1]['status'] == 'error'
    assert events[1]['metadata'] == {}


@pytest.mark.parametrize('mode,expected', [
    ('UNKNOWN', 'UNKNOWN'),
    ('SHARED', 'PROVEN_SHARED'),
    ('DISJOINT', 'PROVEN_DISJOINT'),
])
def test_telemetry_dispatch_metadata(tmp_path, monkeypatch,
                                     mode, expected) -> None:
    monkeypatch.setenv(server.FAST_PATH_ENV, '1')
    repo = _make_repo(tmp_path)
    if mode == 'SHARED':
        _seed_record(repo, 'prior', reads=['target_sh.py'],
                     writes=['target_sh.py'])
        result = _dispatch(repo, 'tsh', writes=['target_sh.py'])
    elif mode == 'DISJOINT':
        _seed_record(repo, 'prior', reads=['other.py'],
                     writes=['other.py'])
        result = _dispatch(repo, 'tdi', writes=['probe_tdi.py'])
    else:
        result = _dispatch(repo, 'tun', writes=['probe_tun.py'])
    payload = _payload(result)
    assert payload['classification'] == expected
    event = _events(repo)[-1]
    assert event['tool'] == 'asha_dispatch_task'
    assert event['status'] == 'ok'
    assert event['metadata']['classification'] == expected
    assert event['metadata']['runtime_mode'] == payload['runtime_mode']
    assert isinstance(event['metadata']['fast_path_enabled'], bool)
    assert isinstance(event['metadata']['timings']['total_ms'], float)


def test_telemetry_never_stores_prompt_or_cmd(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    secret_prompt = 'SECRET_PROMPT_VALUE_DO_NOT_LOG'
    secret_cmd = 'SECRET_CMD_VALUE_DO_NOT_LOG'
    marker = (f'from pathlib import Path; '
              f'Path("probe_p.py").write_text("# p\\n") '
              f'# {secret_cmd}')
    result = _call('asha_dispatch_task', {
        'id': 'priv', 'declared_scope': ['probe_p.py'],
        'reads': ['probe_p.py'], 'writes': ['probe_p.py'],
        'deps': [], 'cmd': [PY, '-c', marker],
        'prompt': secret_prompt, 'root': str(repo)})
    assert not result.get('isError'), result
    raw = _telemetry(repo).read_text(
        encoding='utf-8')
    assert secret_prompt not in raw
    assert secret_cmd not in raw
    for event in _events(repo):
        blob = json.dumps(event).lower()
        for banned in ('password', 'api_key', 'authorization',
                       'secret', 'token', 'cmd', 'prompt'):
            assert f'"{banned}"' not in blob, (banned, event)


def test_telemetry_lines_always_valid_json(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    for index in range(4):
        _call('asha_status', {'root': str(repo)}, request_id=index)
    path = _telemetry(repo)
    lines = path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 4
    ids = [json.loads(line)['request_id'] for line in lines]
    assert ids == ['0', '1', '2', '3']
    for line in lines:
        assert json.loads(line)['tool'] == 'asha_status'


def test_telemetry_failure_never_changes_authorization(tmp_path,
                                                       monkeypatch) -> None:
    monkeypatch.setenv(server.FAST_PATH_ENV, '1')
    repo = _make_repo(tmp_path)
    _seed_record(repo, 'prior', reads=['other.py'], writes=['other.py'])
    # make the telemetry sink unopenable: a DIRECTORY occupies the
    # JSONL name (os.open on a dir fails -> swallowed fail-safe)
    target = _telemetry(repo)
    target.mkdir(parents=True, exist_ok=True)
    result = _dispatch(repo, 'tf', writes=['probe_tf.py'])
    payload = _payload(result)          # call itself unaffected
    assert payload['classification'] == 'PROVEN_DISJOINT'
    assert payload['runtime_mode'] == 'fast_path'  # authorization same
    assert payload['state'] == 'DONE'
    assert not target.is_file()         # no event could be written
