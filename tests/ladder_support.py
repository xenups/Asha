"""Test-only helpers for the Ladder suite.

Nothing here is importable from ``asha``: the fake provider lives in the
test tree on purpose, so no production configuration can select it and
turn a canned response into a shipping approval.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from asha.ladder.providers import Completion

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


def stub_gate(monkeypatch, *, checks: list[dict] | None = None) -> None:
    """Replace the gate's check matrix so tests exercise wiring, not tools.

    Only the runner and the scope resolver are stubbed; the gate's own
    evidence validation, snapshot binding and sealing stay real.
    """
    from asha.governance import ship_gate

    entries = checks if checks is not None else [
        {'name': 'pytest', 'status': 'passed', 'exit_code': 0, 'note': ''}]
    monkeypatch.setattr(ship_gate.check_runner, 'run',
                        lambda root, resolved, **kwargs: list(entries))
    monkeypatch.setattr(ship_gate.scope_resolver, 'resolve',
                        lambda root: {'scope': {}, 'base': 'stub',
                                      'affected_files': ['app.py'],
                                      'checks': ['pytest']})


def finding(**overrides) -> dict:
    """One reviewer finding in the wire contract."""
    payload = {
        'severity': 'HIGH',
        'confidence': 'CONFIRMED',
        'category': 'correctness',
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


def document(*findings: dict, verdict: str = 'APPROVE') -> str:
    return json.dumps({'verdict': verdict, 'findings': list(findings)})


@dataclass
class FakeProvider:
    """Deterministic, test-only provider with per-stage documents."""

    code: str = field(default_factory=lambda: document(verdict='APPROVE'))
    senior: str = field(default_factory=lambda: document(verdict='APPROVE'))
    error: Exception | None = None
    provider: str = 'fake-test-provider'
    model: str = 'fake-1'
    calls: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.provider

    def complete(self, system_prompt: str, user_content: str, *,
                 timeout_s: int) -> Completion:
        stage = ('SENIOR_REVIEW' if 'Senior Reviewer' in system_prompt
                 else 'CODE_REVIEW')
        self.calls.append({'stage': stage, 'system': system_prompt,
                           'user': user_content, 'timeout_s': timeout_s})
        if self.error is not None:
            raise self.error
        return Completion(
            text=self.senior if stage == 'SENIOR_REVIEW' else self.code,
            provider=self.provider, model=self.model, session_id='')
