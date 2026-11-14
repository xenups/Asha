"""Output contract: a passing suite alone never makes a run DONE.

The defect these pin down: a worker that changes nothing leaves the workspace
at the baseline tree, passes every pre-existing test, and was sealed as DONE.

Every case drives ``collect_worker_evidence`` -- the single collection path
both the DAG and the single-worker entrypoints use -- so a passing test here
is a passing gate, not a passing helper.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from asha import evidence
from asha.governance import dag
from asha.governance import output_contract as oc
from asha.governance import worker_execution as we

_PASSING = {'name': 'pytest', 'status': 'passed', 'exit_code': 0,
            'output_tail': '1 passed in 0.01s\n'}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f'git {args} failed: {proc.stderr.strip()}')
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'src').mkdir(parents=True)
    _git(root, 'init', '-q', '.')
    _git(root, 'config', 'user.email', 't@example.invalid')
    _git(root, 'config', 'user.name', 'test')
    _git(root, 'config', 'commit.gpgsign', 'false')
    (root / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n',
                                     encoding='utf-8')
    (root / 'src' / 'mod.py').write_text('VALUE = 1\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'init')
    return root


@pytest.fixture(autouse=True)
def fast_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checks are not what these tests measure; keep them green and instant."""
    monkeypatch.setattr(we.check_runner, 'run',
                        lambda *a, **k: [dict(_PASSING)])
    monkeypatch.setattr(we.check_runner, 'run_scoped',
                        lambda *a, **k: [dict(_PASSING)])


def _worker(script: str, **extra: Any) -> dict[str, Any]:
    worker: dict[str, Any] = {
        'id': 'w1', 'deps': [], 'declared_scope': ['src/', 'REPORT.md',
                                                   'scratch.log'],
        'reads': ['src/mod.py'], 'writes': ['src/mod.py'],
        'cmd': [sys.executable, '-c', script]}
    worker.update(extra)
    return worker


def _collect(repo: Path, tmp_path: Path, worker: dict[str, Any],
             rc: int | None = None
             ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run the worker for real, then collect: the tree is the only witness."""
    tail = ''
    if rc is None:
        rc, tail, _audit = we.execute_with_audit(worker, repo)
    head = _git(repo, 'rev-parse', 'HEAD')
    tree = _git(repo, 'rev-parse', 'HEAD^{tree}')
    outcome = we.collect_worker_evidence(
        worker, repo, rc, tail, 'task-oc', tmp_path / 'evidence',
        base=head, base_tree=tree)
    payload = None
    if outcome.get('evidence'):
        payload = json.loads(Path(outcome['evidence']).read_text(
            encoding='utf-8'))
    return outcome, payload


# ------------------------------------------------------- pure contract rules
def test_changes_contract_needs_one_declared_write() -> None:
    worker = {'writes': ['src/mod.py']}
    assert oc.declared_kind(worker) == 'changes'
    empty = oc.evaluate(worker, observed=[], writes_observed=[])
    assert empty['satisfied'] is False
    assert empty['reason'] == 'no_change_in_declared_writes'
    assert oc.evaluate(worker, observed=['src/mod.py'],
                       writes_observed=['src/mod.py'])['satisfied'] is True


def test_unknown_kind_is_refused() -> None:
    record = oc.evaluate({'output_kind': 'vibes'}, observed=[], writes_observed=[])
    assert record['satisfied'] is False
    assert record['reason'] == 'unknown_output_kind:vibes'


def test_kind_is_derived_from_declared_fields() -> None:
    assert oc.declared_kind({'required_artifacts': ['r.md']}) == 'artifact'
    assert oc.declared_kind({'no_change_reason': 'audit only'}) == 'no_change'
    assert oc.declared_kind({}) == 'uncontracted'


# ------------------------------------------------------------- integration
def test_coding_task_with_changes_is_done(repo: Path, tmp_path: Path) -> None:
    script = ("import pathlib; p = pathlib.Path('src/mod.py');"
              "p.write_text(p.read_text() + 'VALUE2 = 2\\n')")
    outcome, payload = _collect(repo, tmp_path, _worker(script))
    assert outcome['state'] == 'DONE'
    assert payload is not None
    assert payload['output_contract']['satisfied'] is True
    assert payload['writes_observed'] == ['src/mod.py']


def test_coding_task_with_no_changes_fails(repo: Path, tmp_path: Path) -> None:
    """Tests pass, tree untouched: not DONE, and the seal may not claim PASS."""
    outcome, payload = _collect(repo, tmp_path, _worker('pass'))
    assert outcome['state'] == 'FAILED'
    assert outcome['reason'] == (
        'output_contract_not_satisfied:no_change_in_declared_writes')
    assert payload is not None                 # the record survives for audit
    assert payload['validation_status'] == 'FAIL'
    assert payload['output_contract']['satisfied'] is False
    assert payload['evidence_sha256'] == evidence.compute_digest(payload)


def test_unrelated_temporary_file_is_not_output(repo: Path, tmp_path: Path
                                               ) -> None:
    script = ("import pathlib; pathlib.Path('scratch.log').write_text('noise')")
    outcome, _payload = _collect(repo, tmp_path, _worker(script))
    assert outcome['state'] == 'FAILED'
    assert 'no_change_in_declared_writes' in outcome['reason']


def test_untracked_source_file_counts(repo: Path, tmp_path: Path) -> None:
    script = "import pathlib; pathlib.Path('src/added.py').write_text('X = 1\\n')"
    worker = _worker(script, writes=['src/added.py'])
    outcome, payload = _collect(repo, tmp_path, worker)
    assert outcome['state'] == 'DONE'
    assert payload is not None
    assert payload['writes_observed'] == ['src/added.py']


def test_deletion_is_recorded(repo: Path, tmp_path: Path) -> None:
    script = "import pathlib; pathlib.Path('src/mod.py').unlink()"
    outcome, payload = _collect(repo, tmp_path, _worker(script))
    assert outcome['state'] == 'DONE'
    assert payload is not None
    assert payload['writes_observed'] == ['src/mod.py']
    assert '-VALUE = 1' in payload['diff']


def test_report_artifact_written_is_done(repo: Path, tmp_path: Path) -> None:
    script = ("import pathlib; pathlib.Path('REPORT.md')"
              ".write_text('# Review\\n\\nH1 verified.\\n')")
    worker = _worker(script, writes=[], required_artifacts=['REPORT.md'])
    outcome, payload = _collect(repo, tmp_path, worker)
    assert outcome['state'] == 'DONE'
    assert payload is not None
    artifact = payload['output_contract']['artifacts'][0]
    assert artifact['path'] == 'REPORT.md'
    assert artifact['written_this_run'] is True
    assert artifact['size'] and artifact['sha256']


def test_report_printed_but_not_written_fails(repo: Path, tmp_path: Path
                                              ) -> None:
    """The exact failure mode that started this: prose in chat, no file."""
    worker = _worker('print("# Review: H1 verified.")', writes=[],
                     required_artifacts=['REPORT.md'])
    outcome, payload = _collect(repo, tmp_path, worker)
    assert outcome['state'] == 'FAILED'
    assert outcome['reason'] == (
        'output_contract_not_satisfied:artifact_missing_or_empty:REPORT.md')
    assert payload is not None
    assert payload['validation_status'] == 'FAIL'
    assert payload['output_contract']['artifacts'][0]['exists'] is False


def test_empty_artifact_fails(repo: Path, tmp_path: Path) -> None:
    script = "import pathlib; pathlib.Path('REPORT.md').write_text('')"
    worker = _worker(script, writes=[], required_artifacts=['REPORT.md'])
    outcome, _payload = _collect(repo, tmp_path, worker)
    assert outcome['state'] == 'FAILED'
    assert 'artifact_missing_or_empty' in outcome['reason']


def test_no_change_contract_with_reason_is_done(repo: Path, tmp_path: Path
                                               ) -> None:
    worker = _worker('pass', writes=[], no_change_reason='audit only')
    outcome, payload = _collect(repo, tmp_path, worker)
    assert outcome['state'] == 'DONE'
    assert payload is not None
    assert payload['output_contract']['reason_text'] == 'audit only'


def test_no_change_contract_needs_a_reason(repo: Path, tmp_path: Path) -> None:
    worker = _worker('pass', writes=[], output_kind='no_change')
    outcome, _payload = _collect(repo, tmp_path, worker)
    assert outcome['state'] == 'FAILED'
    assert 'no_change_without_reason' in outcome['reason']


def test_no_change_contract_rejects_a_change(repo: Path, tmp_path: Path) -> None:
    script = "import pathlib; pathlib.Path('src/mod.py').write_text('X = 2\\n')"
    worker = _worker(script, writes=[], no_change_reason='audit only')
    outcome, _payload = _collect(repo, tmp_path, worker)
    assert outcome['state'] == 'FAILED'
    assert 'changed_files_under_no_change_contract' in outcome['reason']


def test_worker_exit_failure_is_reported_first(repo: Path, tmp_path: Path
                                               ) -> None:
    outcome, payload = _collect(repo, tmp_path, _worker('pass'), rc=4)
    assert outcome['state'] == 'FAILED'
    assert outcome['exit_status'] == 4
    assert payload is None


def test_dag_and_single_worker_agree(repo: Path, tmp_path: Path) -> None:
    """Parity: the same no-op task fails identically on both entrypoints."""
    worker = _worker('pass')

    single = we.run_worker_in_worktree(repo, dict(worker), task_id='parity-1')
    run = dag.run_workers_dag(repo, [dict(worker)], task_id='parity-2')
    single_state = single['states']['w1']
    dag_state = run['states']['w1']
    assert single_state['state'] == dag_state['state'] == 'FAILED'
    assert single_state['reason'].startswith('output_contract_not_satisfied:')
    assert dag_state['reason'].startswith('output_contract_not_satisfied:')
    # Both paths seal the honest record; neither may call the run a success.
    assert single['evidence'].get('w1') and run['evidence'].get('w1')
    assert single_state['validation_status'] == 'FAIL'
    assert dag_state['validation_status'] == 'FAIL'
