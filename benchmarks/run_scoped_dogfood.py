"""Phase 5.0 -- real repository dogfooding (spec section 9) plus the
real-repo cost model (section 8).

Three genuine asha_dispatch_task runs through the real in-process MCP
handler with ASHA_FAST_PATH_ENABLED=1:

  1. eligible leaf change (.jspace probe, no observers)
     -> expect validation_mode=SCOPED with machine-readable proof
  2. equivalent leaf change, engine FORCED to COMPLETE (offline
     differential technique, not a runtime path)
     -> expect validation_mode=COMPLETE fallback=FORCED_DIFFERENTIAL
     -> this is the same-class COMPLETE cost for the speedup number
  3. deliberately ineligible change (public surface of pkg/__init__.py)
     -> expect validation_mode=COMPLETE fallback=FORBIDDEN_SCOPE_LEVEL

Cleanup resets every dogfood commit so the working tree ends clean.
No benchmark-only execution path is used: all three runs go through
asha_dispatch_task exactly as a client would.

Run: python benchmarks/run_scoped_dogfood.py   (~13 minutes)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import asha.mcp_server as server
from asha import scoping

PY = sys.executable
REPO = Path(__file__).resolve().parent.parent
ENV = server.FAST_PATH_ENV
PROBE1 = '.jspace/scoped_dogfood_probe.py'
PROBE2 = '.jspace/scoped_dogfood_probe2.py'
S4_TARGET = '.github/workflows/ci.yml'
PROBE_CODE = 'def _dogfood_probe():\n    return 1\n'


def _git(*args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=REPO, capture_output=True,
                          text=True, check=True, timeout=120)
    return proc.stdout.strip()


def _status() -> str:
    proc = subprocess.run(['git', 'status', '--porcelain'], cwd=REPO,
                          capture_output=True, text=True, check=True,
                          timeout=120)
    return proc.stdout


def _dispatch(wid: str, *, scope: list[str], writes: list[str],
              cmd_code: str, env_on: bool = True,
              reads: list[str] | None = None) -> dict[str, Any]:
    if env_on:
        os.environ[ENV] = '1'
    else:
        os.environ.pop(ENV, None)
    cmd = [PY, '-c', cmd_code]
    arguments: dict[str, Any] = {
        'id': wid, 'declared_scope': scope, 'reads': reads,
        'writes': writes, 'deps': [], 'cmd': cmd, 'root': str(REPO),
    }
    response = server.handle_message({
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'asha_dispatch_task', 'arguments': arguments}})
    assert response is not None and 'result' in response, response
    result = response['result']
    assert not result['isError'], result['content'][0]['text']
    return json.loads(result['content'][0]['text'])


def _evidence(task_id: str, wid: str) -> dict[str, Any]:
    path = (REPO / '.jspace' / 'cache' / 'orchestrator' / task_id
            / f'{wid}.json')
    assert path.is_file(), f'missing sealed evidence: {path}'
    return json.loads(path.read_text(encoding='utf-8'))


def _last_telemetry() -> dict[str, Any]:
    path = REPO / '.jspace' / 'mcp_live_telemetry.jsonl'
    lines = [line for line in path.read_text(encoding='utf-8').splitlines()
             if line.strip()]
    event = json.loads(lines[-1])
    return event.get('metadata', {})


def _write_cmd(rel: str, content: str) -> str:
    # newlines must be ESCAPED for embedding inside a python -c literal
    escaped = (content.replace('\\', '\\\\')
               .replace('\n', '\\n').replace("'", "\\'"))
    return (f"import pathlib;p=pathlib.Path('{rel}');"
            f"p.write_text('{escaped}',encoding='utf-8')")


def _append_cmd(rel: str, content: str) -> str:
    # newlines must be ESCAPED for embedding inside a python -c literal
    escaped = (content.replace('\\', '\\\\')
               .replace('\n', '\\n').replace("'", "\\'"))
    return (f"import pathlib;p=pathlib.Path('{rel}');"
            f"p.write_text(p.read_text(encoding='utf-8')+'{escaped}',"
            f"encoding='utf-8')")


def _checks_summary(payload: dict) -> dict[str, str]:
    return {entry['name']: entry['status']
            for entry in payload.get('checks', [])}


def _timings(label: str, payload: dict,
             eligibility_ms: float) -> None:
    meta = _last_telemetry()
    timings = meta.get('timings', {})
    checks = payload.get('checks', [])
    validation_ms = sum(entry.get('duration_ms', 0) for entry in checks)
    print(f'  {label}:')
    print(f"    T_classify={timings.get('classification_ms', 0):.3f}ms "
          f"T_route={timings.get('routing_ms', 0):.3f}ms "
          f'T_eligibility={eligibility_ms:.0f}ms')
    evidence_ms = timings.get('evidence_ms', 0)
    total_ms = timings.get('total_ms', 0)
    wall_ms = meta.get('duration_ms', 0)
    print(f'    T_validation={validation_ms:.0f}ms '
          f'T_evidence={evidence_ms:.0f}ms '
          f'T_total={total_ms:.0f}ms '
          f'(wall telemetry duration_ms={wall_ms:.0f})')


def main() -> int:
    if _status():
        print('REFUSED: working tree is not clean before dogfooding')
        return 1
    pre_sha = _git('rev-parse', 'HEAD')
    print(f'pre_sha={pre_sha} env={ENV}=1')
    failures: list[str] = []
    report: dict[str, Any] = {}

    # -- case 1: genuinely eligible leaf change -------------------------
    print('-- case 1: eligible leaf probe (expect SCOPED)', flush=True)
    payload = _dispatch(
        'scoped-e1', scope=[PROBE1], writes=[PROBE1],
        cmd_code=_write_cmd(PROBE1, PROBE_CODE), env_on=True)
    # eligibility measured standalone via the real engine on the same
    # change (the dispatch embeds it; timing read here). The dispatched
    # run executes in an isolated worktree that is removed afterwards,
    # so the probe change is materialized on the main repo root for the
    # measurement and immediately removed -- the tree stays clean.
    probe_main = REPO / PROBE1
    probe_main.parent.mkdir(parents=True, exist_ok=True)
    probe_main.write_text(PROBE_CODE, encoding='utf-8')
    try:
        t0 = time.perf_counter()
        decision = scoping.assess_scoping_eligibility(
            REPO, [PROBE1], 'PROVEN_DISJOINT', 'S1')
        eligibility_ms = (time.perf_counter() - t0) * 1000
    finally:
        probe_main.unlink(missing_ok=True)
    ev1 = _evidence(payload.get('task_id', 'dispatch-scoped-e1'),
                    'scoped-e1')
    mode1 = ev1.get('validation_mode')
    print(f'   state={payload["state"]} classification='
          f'{payload["classification"]} mode={payload["runtime_mode"]} '
          f'validation_mode={mode1} '
          f'fallback={ev1.get("fallback_reason")} '
          f'checks={_checks_summary(ev1)}')
    if payload['state'] != 'DONE':
        failures.append(f'case1 state {payload["state"]}')
    if mode1 != 'SCOPED':
        failures.append(f'case1 validation_mode {mode1!r}')
    if ev1.get('fallback_reason'):
        failures.append(f'case1 unexpected fallback {ev1}')
    if decision.eligible is False:
        failures.append(f'engine disagrees on eligibility: '
                        f'{decision.fallback_reason}')
    pytest_entry = next(e for e in ev1['checks'] if e['name'] == 'pytest')
    if pytest_entry['status'] != 'skipped' or \
            pytest_entry.get('note') != 'empty_target_set_proven':
        failures.append(f'case1 pytest entry {pytest_entry}')
    _timings('SCOPED', ev1, eligibility_ms)
    report['eligible'] = {'validation_mode': mode1,
                          'checks': _checks_summary(ev1),
                          'targeted_tests': ev1.get('targeted_tests'),
                          'mypy_targets': ev1.get('mypy_targets')}

    # -- case 2: equivalent leaf, engine FORCED to COMPLETE -------------
    print('-- case 2: equivalent leaf, forced COMPLETE (section 8 pair)',
          flush=True)
    original = scoping.assess_scoping_eligibility
    scoping.assess_scoping_eligibility = (  # type: ignore[assignment]
        lambda *_a, **_k: scoping.complete_decision(
            'FORCED_DIFFERENTIAL'))
    try:
        started = time.perf_counter()
        payload = _dispatch(
            'scoped-e2', scope=[PROBE2], writes=[PROBE2],
            cmd_code=_write_cmd(PROBE2, PROBE_CODE), env_on=True)
        # engine forced offline: no eligibility phase was exercised
        eligibility_ms = 0.0
        del started
    finally:
        scoping.assess_scoping_eligibility = original  # type: ignore[assignment]
    ev2 = _evidence(payload.get('task_id', 'dispatch-scoped-e2'),
                    'scoped-e2')
    mode2 = ev2.get('validation_mode')
    print(f'   state={payload["state"]} validation_mode={mode2} '
          f'fallback={ev2.get("fallback_reason")} '
          f'checks={_checks_summary(ev2)}')
    if mode2 != 'COMPLETE' or \
            ev2.get('fallback_reason') != 'FORCED_DIFFERENTIAL':
        failures.append(f'case2 validation_mode={mode2} '
                        f'fallback={ev2.get("fallback_reason")}')
    _timings('COMPLETE(equivalent)', ev2, eligibility_ms)
    report['equivalent'] = {'validation_mode': mode2,
                            'checks': _checks_summary(ev2)}

    # -- case 3: deliberately ineligible (S4 gate/control surface) ------
    # .github/workflows/ci.yml -> classify_path S4 (static, certain),
    # one of the spec 2.3 hard lockouts
    print('-- case 3: ineligible S4 change (expect COMPLETE + reason)',
          flush=True)
    payload = _dispatch(
        'scoped-e3', scope=[S4_TARGET],
        writes=[S4_TARGET],
        cmd_code=_append_cmd(S4_TARGET,
                             '\n# phase50 dogfood: ineligible surface\n'),
        env_on=True)
    ev3 = _evidence(payload.get('task_id', 'dispatch-scoped-e3'),
                    'scoped-e3')
    mode3 = ev3.get('validation_mode')
    reason3 = ev3.get('fallback_reason')
    print(f'   state={payload["state"]} validation_mode={mode3} '
          f'fallback={reason3} checks={_checks_summary(ev3)}')
    if payload['state'] != 'DONE':
        failures.append(f'case3 state {payload["state"]}')
    if mode3 != 'COMPLETE':
        failures.append(f'case3 validation_mode {mode3!r}')
    if reason3 != scoping.F_FORBIDDEN_SCOPE_LEVEL:
        failures.append(f'case3 fallback {reason3!r}')
    _timings('COMPLETE(ineligible)', ev3, 0.0)
    report['ineligible'] = {'validation_mode': mode3,
                            'fallback_reason': reason3,
                            'checks': _checks_summary(ev3)}

    # -- cleanup: drop every dogfood commit, tree back to pre-sha -------
    _git('reset', '--hard', pre_sha)
    leftover = _status()
    if leftover:
        failures.append(f'working tree not clean after cleanup: '
                        f'{leftover!r}')
    print(f'cleanup: reset --hard {pre_sha[:10]}, '
          f'tree_clean={not bool(leftover)}')

    # -- verdict ---------------------------------------------------------
    print('=' * 72)
    print('PHASE 5.0 DOGFOOD (section 9)')
    print(f'eligible leaf task:        '
          f'{report["eligible"]["validation_mode"]}')
    print(f'ineligible task:           '
          f'{report["ineligible"]["validation_mode"]} '
          f'({report["ineligible"]["fallback_reason"]})')
    print(f'equivalent COMPLETE pair:  '
          f'{report["equivalent"]["validation_mode"]} '
          f'({report["equivalent"].get("fallback_reason", "")})')
    print('runtime evidence verified: '
          f'{"YES" if all(r.get("checks") for r in report.values()) else "NO"}')
    print(f'working tree clean:        '
          f'{"YES" if not _status() else "NO"}')
    if failures:
        print('DOGFOOD RESULT: FAILED')
        for item in failures:
            print(f'  !! {item}')
        return 1
    print('DOGFOOD RESULT: ALL CASES MATCH EXPECTATIONS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
