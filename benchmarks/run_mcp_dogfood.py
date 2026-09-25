"""Phase 4.3 -- dogfooding: the new MCP tools run against the Asha
repository itself, through the real in-process handler (no MCP
subprocess), with reversible probes only.

Probes write ignored files under <repo>/.jspace/, so the working tree
stays clean for the whole run; scenario results are checked against
HAND-WRITTEN expectations and reported as facts. Each dispatch runs
the repository's own check matrix (ruff/pytest/mypy) through the
governed path -- this script takes minutes, not seconds, on purpose:
nothing is skipped to look fast.

Run: PYTHONPATH=. python benchmarks/run_mcp_dogfood.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import asha.mcp_server as server

PY = sys.executable
REPO = Path(__file__).resolve().parent.parent
ENV = server.FAST_PATH_ENV


def _dispatch(wid: str, *, scope: list[str], reads: list[str] | None,
              writes: list[str], env_on: bool) -> dict[str, Any]:
    if env_on:
        os.environ[ENV] = '1'
    else:
        os.environ.pop(ENV, None)
    cmd = [PY, '-c',
           ('from pathlib import Path; '
            f'Path({writes[0]!r}).write_text("dogfood {wid}\\n")')]
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


def _status() -> str:
    proc = subprocess.run(['git', 'status', '--porcelain'], cwd=REPO,
                          capture_output=True, text=True, timeout=60,
                          check=True)
    return proc.stdout


def main() -> int:
    if _status():
        print('REFUSED: working tree is not clean before dogfooding')
        return 1
    offset = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    probes = [f'.jspace/dogfood_probe_{offset + index}.txt'
              for index in range(5)]
    # hand-written expectations (checked, never derived from output).
    # Two tables: a fresh envelope (no recorded executions -> prime is
    # a natural UNKNOWN) versus an envelope with history (prime
    # classifies against real recorded surfaces)
    has_history = bool(list(
        (REPO / '.jspace' / 'cache' / 'orchestrator')
        .rglob('*.json')))
    prime_expectation = ('UNKNOWN', 'full_governance') \
        if not has_history else ('PROVEN_DISJOINT', 'full_governance')
    expected: dict[str, tuple[str, str]] = {
        'prime': prime_expectation,
        'dogfood-a0': ('PROVEN_DISJOINT', 'full_governance'),
        'dogfood-a1': ('PROVEN_DISJOINT', 'fast_path'),
        'dogfood-b': ('PROVEN_SHARED', 'full_governance'),
        'dogfood-c': ('UNKNOWN', 'full_governance'),
    }
    print(f'history_records={has_history} probe_offset={offset}')
    results: dict[str, dict[str, Any]] = {}
    plan: list[tuple[str, dict[str, Any]]] = [
        # prime: fresh envelope -> missing_context -> UNKNOWN expected
        # (or DISJOINT when history exists; table picked above)
        ('prime', {'scope': [probes[0]], 'reads': [probes[0]],
                   'writes': [probes[0]], 'env_on': False}),
        # A: disjoint from every recorded surface, flag OFF then ON
        ('dogfood-a0', {'scope': [probes[1]], 'reads': [probes[1]],
                        'writes': [probes[1]], 'env_on': False}),
        ('dogfood-a1', {'scope': [probes[2]], 'reads': [probes[2]],
                        'writes': [probes[2]], 'env_on': True}),
        # B: real overlap with prime's recorded surface, flag ON
        ('dogfood-b', {'scope': [probes[0]], 'reads': [probes[0]],
                       'writes': [probes[0]], 'env_on': True}),
        # C: undeclared read surface -> natural UNKNOWN, flag ON
        ('dogfood-c', {'scope': [probes[3]], 'reads': None,
                       'writes': [probes[3]], 'env_on': True}),
    ]
    for wid, kwargs in plan:
        print(f'-- dispatch {wid} (env={"1" if kwargs["env_on"] else "0"})',
              flush=True)
        results[wid] = _dispatch(wid, **kwargs)
        payload = results[wid]
        print(f'   classification={payload["classification"]} '
              f'mode={payload["runtime_mode"]} '
              f'reason={payload["routing_reason"]} '
              f'state={payload["state"]} '
              f'worktrees={len(payload["worktrees"])} '
              f'evidence={"yes" if payload["evidence"] else "no"}',
              flush=True)

    # safety metrics against the hand-written table
    false_fast_path = sum(
        1 for wid, payload in results.items()
        if payload['runtime_mode'] == 'fast_path'
        and expected[wid][1] != 'fast_path')
    mismatch = [(wid, payload['classification'],
                 expected[wid][0], payload['runtime_mode'],
                 expected[wid][1])
                for wid, payload in results.items()
                if (payload['classification'], payload['runtime_mode'])
                != expected[wid]]
    fast_checked = sum(1 for _wid, kwargs in plan
                       if kwargs['env_on'])
    fast_taken = sum(1 for payload in results.values()
                     if payload['runtime_mode'] == 'fast_path')
    unknown_never_fast = all(
        payload['runtime_mode'] != 'fast_path'
        for payload in results.values()
        if payload['classification'] == 'UNKNOWN')
    shared_never_fast = all(
        payload['runtime_mode'] != 'fast_path'
        for payload in results.values()
        if payload['classification'] == 'PROVEN_SHARED')

    # live performance (§12): real numbers only
    print('== timings (ms, live) ==')
    for wid, payload in results.items():
        timings = payload['timings']
        print(f'{wid:<14} classify={timings["classification_ms"]:.3f} '
              f'route={timings["routing_ms"]:.3f} '
              f'scheduler={timings["scheduler_ms"]:.1f} '
              f'execution={timings["execution_ms"]:.1f} '
              f'evidence={timings["evidence_ms"]:.1f} '
              f'total={timings["total_ms"]:.1f}')

    # telemetry sample: structured events only (metadata carries no
    # prompt and no cmd by construction)
    telemetry = REPO / '.jspace' / 'mcp_live_telemetry.jsonl'
    if telemetry.is_file():
        events = [json.loads(line) for line in
                  telemetry.read_text(encoding='utf-8').splitlines()
                  if line.strip()]
        print(f'== telemetry sample (last 3 of {len(events)}) ==')
        for event in events[-3:]:
            print(json.dumps(event, sort_keys=True))

    print('== safety ==')
    print(f'fast_path_checked={fast_checked}')
    print(f'fast_path_taken={fast_taken}')
    print(f'false_fast_path={false_fast_path}')
    print(f'unknown_never_fast={unknown_never_fast}')
    print(f'shared_never_fast={shared_never_fast}')
    print(f'expectation_mismatches={mismatch}')

    # cleanup: probes are ignored, tree was clean before and must be
    # clean after
    for probe in probes:
        path = REPO / probe
        if path.exists():
            path.unlink()
    leftover = _status()
    print(f'git_status_after_cleanup={leftover!r}')
    return 0 if (not mismatch and false_fast_path == 0
                 and not leftover) else 1


if __name__ == '__main__':
    raise SystemExit(main())
