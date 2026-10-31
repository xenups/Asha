"""Test-only helpers for the Ladder suite.

This harness acts as the MCP client: it speaks the real ``tools/call``
JSON-RPC framing and submits fixtures that stand in for a host LLM's
reasoning. Nothing here is importable from ``asha``, and no fixture can be
selected by production configuration, so a canned submission can never
become a shipping approval outside a test.
"""

from __future__ import annotations

import json
import subprocess
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_SOURCE = 'def add(a, b):\n    return a + b\n'
CHANGED_SOURCE = ('def add(a, b):\n    return a - b\n\n\n'
                  'def sub(a, b):\n    return a - b\n')


def git(root: Path, *argv: str) -> str:
    return subprocess.run(['git', *argv], cwd=root, check=True,
                          capture_output=True, text=True).stdout


@dataclass
class Repo:
    """A scratch repository with exactly one reviewable change."""

    root: Path
    base: str
    head: str


def make_repo(tmp_path: Path, *, changed: str = CHANGED_SOURCE) -> Repo:
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.email', 'test@example.invalid')
    git(root, 'config', 'user.name', 'Test')
    (root / 'app.py').write_text(BASE_SOURCE, encoding='utf-8')
    (root / 'tests').mkdir()
    (root / 'tests' / 'test_app.py').write_text(
        'from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n',
        encoding='utf-8')
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'base')
    (root / 'app.py').write_text(changed, encoding='utf-8')
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'change')
    return Repo(root=root,
                base=git(root, 'rev-parse', 'HEAD~1').strip(),
                head=git(root, 'rev-parse', 'HEAD').strip())


@dataclass
class GateStub:
    """Records gate invocations and returns a fixed sealed decision."""

    authorized: bool = True
    gate_state: str = 'AUTHORIZED'
    reasons: tuple[str, ...] = ()
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def gate_ship(self, root: Path, *, reviewer: Any = None,
                  no_execute: bool = False) -> dict[str, Any]:
        findings = reviewer(root, 'snap-test', 30)['findings'] if reviewer else []
        self.calls.append({'root': str(root), 'findings': findings})
        if not self.authorized:
            raise RuntimeError('gate refused (stub)')
        return {
            'gate_state': self.gate_state,
            'authorized_to_ship': True,
            'gate_reasons': list(self.reasons),
            'blocking_findings': [],
            'policy_version': 'stub',
            'snapshot_id': 'snap-test',
        }


def stub_gate(monkeypatch: Any, *, authorized: bool = True,
              gate_state: str = 'AUTHORIZED') -> GateStub:
    """Swap the canonical gate for a recorder; assertions stay ours."""
    from asha.ladder import orchestrator

    stub = GateStub(authorized=authorized, gate_state=gate_state)
    monkeypatch.setattr(orchestrator, 'ship_gate', types.SimpleNamespace(
        gate_ship=stub.gate_ship, POLICY_VERSION='stub'))
    return stub


def finding(**overrides: Any) -> dict[str, Any]:
    """One reviewer finding in the wire contract."""
    payload: dict[str, Any] = {
        'finding_id': 'CR-1',
        'category': 'correctness',
        'severity': 'HIGH',
        'confidence': 'CONFIRMED',
        'title': 'add() returns a difference',
        'description': 'add subtracts instead of adding',
        'file_path': 'app.py',
        'line_start': 2,
        'line_end': 2,
        'evidence': 'test_add asserts 3 but add(1, 2) is -1',
        'impact': 'every caller receives a wrong sum',
        'recommended_action': 'return a + b',
        'blocking': True,
    }
    payload.update(overrides)
    return payload


def call(name: str, **arguments: Any) -> dict[str, Any]:
    """One real JSON-RPC ``tools/call``; returns the MCP result envelope."""
    from asha import mcp_server

    frame = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}}
    response = mcp_server.handle_message(dict(frame))
    assert response is not None and "result" in response, response
    return response["result"]


def tool(name: str, **arguments: Any) -> dict[str, Any]:
    """Call a tool and return its JSON payload (raises on a tool error)."""
    result = call(name, **arguments)
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def tool_error(name: str, **arguments: Any) -> str:
    """Call a tool expecting a typed refusal; returns the error text."""
    result = call(name, **arguments)
    assert result["isError"] is True, result["content"][0]["text"]
    return result["content"][0]["text"]


def start_review_via_mcp(repo: Repo, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {'root': str(repo.root), 'base': repo.base,
                                 'head': repo.head}
    arguments.update(overrides)
    return tool('asha_review', **arguments)


def submit_via_mcp(repo: Repo, run_id: str, packet: dict[str, Any], *,
                   verdict: str = 'APPROVE',
                   findings: list[dict[str, Any]] | None = None,
                   summary: str = 'reviewed the diff',
                   **metadata: Any) -> dict[str, Any]:
    """Submit one stage the way a host LLM would."""
    payload: dict[str, Any] = {
        'stage': packet['stage'],
        'stage_id': packet['stage_id'],
        'verdict': verdict,
        'summary': summary,
        'findings': findings if findings is not None else [],
    }
    if metadata:
        payload['reviewer_metadata'] = metadata
    return tool('asha_review_submit_stage', root=str(repo.root),
                review_run_id=run_id, **payload)


def next_stage(repo: Repo, run_id: str) -> dict[str, Any]:
    return tool('asha_review_next_stage', root=str(repo.root),
                review_run_id=run_id)


def finalize(repo: Repo, run_id: str) -> dict[str, Any]:
    return tool('asha_review_finalize', root=str(repo.root),
                review_run_id=run_id)


def status(repo: Repo, run_id: str) -> dict[str, Any]:
    return tool('asha_review_status', root=str(repo.root), run_id=run_id)
