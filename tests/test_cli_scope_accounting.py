"""M2 -- explicit ``--paths`` must never hide a discovered change.

The gate may be told which paths to examine; it may not be told to
forget the ones that changed. These tests pin the observable contract:

* a discovered changed path that is not named -> exit 2, SCOPE_INCOMPLETE
* complete accounting -> success, ``unaccounted_paths`` is an empty list
* repeating ``--paths`` -> usage error (argparse exit 2), never last-wins
* comma form -> split into paths, empty entry -> refusal (never dropped)
* deletions and renames -> accounted through the real Git snapshot
* ``unaccounted_paths`` is present on every emitted payload
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from asha import cli


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f'git {args} failed: {proc.stderr.strip()}')
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'tests').mkdir(parents=True)
    _git(root, 'init', '-q', '.')
    _git(root, 'config', 'user.email', 't@example.invalid')
    _git(root, 'config', 'user.name', 'test')
    (root / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n', encoding='utf-8')
    (root / 'notes.md').write_text('# notes\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'init')
    return root


def _run(root: Path, *flags: str, capsys: Any) -> tuple[int, str, str]:
    code = cli.main(['--root', str(root), *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _three_changes(root: Path) -> list[str]:
    """Modify one tracked file, add one untracked file, modify a second
    tracked file -- three discovered paths, none of them staged."""
    (root / 'tests' / 'test_thing.py').write_text(
        'def test_ok():\n    assert True\n# change\n', encoding='utf-8')
    (root / 'notes.md').write_text('# notes\nmore\n', encoding='utf-8')
    (root / 'extra.py').write_text('x = 1\n', encoding='utf-8')
    return ['extra.py', 'notes.md', 'tests/test_thing.py']


# --------------------------------------------------- discovered vs declared
def test_three_changed_paths_with_one_supplied_is_refused(
        repo: Path, capsys: Any) -> None:
    _three_changes(repo)
    code, out, err = _run(repo, '--paths', 'extra.py', '--json',
                          '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['error'] == 'SCOPE_INCOMPLETE'
    assert payload['unaccounted_paths'] == ['notes.md', 'tests/test_thing.py']
    assert payload['changed_files'] == []          # nothing was validated
    assert 'notes.md' in err and 'tests/test_thing.py' in err


def test_omitted_changed_path_fails_closed(repo: Path, capsys: Any) -> None:
    (repo / 'extra.py').write_text('x = 1\n', encoding='utf-8')
    (repo / 'notes.md').write_text('# notes\nmore\n', encoding='utf-8')
    code, out, _err = _run(repo, '--paths', 'extra.py', '--json',
                           '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['error'] == 'SCOPE_INCOMPLETE'
    assert payload['unaccounted_paths'] == ['notes.md']


def test_all_discovered_paths_accounted_succeeds(repo: Path,
                                                 capsys: Any) -> None:
    expected = _three_changes(repo)
    code, out, err = _run(repo, '--paths', *expected, '--json',
                          '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert err == ''
    assert payload['unaccounted_paths'] == []
    assert sorted(payload['changed_files']) == expected


def test_auto_discovery_reports_complete_accounting(repo: Path,
                                                    capsys: Any) -> None:
    expected = _three_changes(repo)
    code, out, _err = _run(repo, '--json', '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload['unaccounted_paths'] == []
    assert sorted(payload['changed_files']) == expected


# ------------------------------------------------------------- --paths form
def test_repeated_paths_flag_is_a_usage_error(repo: Path,
                                             capsys: Any) -> None:
    _three_changes(repo)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(['--root', str(repo), '--paths', 'extra.py',
                  '--paths', 'notes.md'])
    captured = capsys.readouterr()
    assert excinfo.value.code == cli.EXIT_ERROR
    assert '--paths may be given only once' in captured.err


def test_comma_separated_paths_are_split(repo: Path, capsys: Any) -> None:
    expected = _three_changes(repo)
    code, out, _err = _run(repo, '--paths', ','.join(expected), '--json',
                           '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload['unaccounted_paths'] == []
    assert sorted(payload['changed_files']) == expected


def test_empty_path_entry_is_rejected(repo: Path, capsys: Any) -> None:
    _three_changes(repo)
    code, out, err = _run(repo, '--paths', 'extra.py,,notes.md', '--json',
                          '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['error'] == 'INVALID_ARGUMENTS'
    assert 'empty path entry' in err
    assert payload['unaccounted_paths'] == []


def test_duplicate_paths_are_folded(repo: Path, capsys: Any) -> None:
    (repo / 'extra.py').write_text('x = 1\n', encoding='utf-8')
    code, out, _err = _run(repo, '--paths', 'extra.py', 'extra.py', '--json',
                           '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload['changed_files'] == ['extra.py']
    assert payload['unaccounted_paths'] == []


# --------------------------------------------------------- git-side shapes
def test_deleted_path_is_accounted(repo: Path, capsys: Any) -> None:
    _git(repo, 'rm', '-q', 'notes.md')
    code, out, _err = _run(repo, '--paths', 'extra.py', '--json',
                           '--no-execute', capsys=capsys)
    # extra.py does not exist; the only discovered change is the deletion
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['unaccounted_paths'] == ['notes.md']

    code, out, _err = _run(repo, '--paths', 'notes.md', '--json',
                           '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload['unaccounted_paths'] == []
    assert payload['changed_files'] == ['notes.md']


def test_rename_accounts_for_both_sides(repo: Path, capsys: Any) -> None:
    _git(repo, 'mv', 'notes.md', 'renamed.md')
    code, out, _err = _run(repo, '--paths', 'renamed.md', '--json',
                           '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['unaccounted_paths'] == ['notes.md']

    code, out, _err = _run(repo, '--paths', 'notes.md', 'renamed.md',
                           '--json', '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload['unaccounted_paths'] == []
    assert sorted(payload['changed_files']) == ['notes.md', 'renamed.md']


# ------------------------------------------------- payload + fail-closed
def test_no_changes_payload_carries_empty_unaccounted(repo: Path,
                                                      capsys: Any) -> None:
    code, out, _err = _run(repo, '--json', '--no-execute', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK
    assert payload['status'] == 'NO_CHANGES'
    assert payload['unaccounted_paths'] == []


def test_accounting_refusal_never_reaches_the_sealed_runner(
        repo: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _three_changes(repo)

    def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError('sealed runner must not run on refused scope')

    monkeypatch.setattr(cli, '_run_sealed', _boom)
    code, out, _err = _run(repo, '--paths', 'extra.py', '--json',
                           capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['error'] == 'SCOPE_INCOMPLETE'
    assert payload['validation_result'] is None
    assert payload['evidence_id'] is None


def test_error_payload_carries_unaccounted_paths(repo: Path,
                                                 capsys: Any) -> None:
    code, out, _err = _run(repo, '--paths', str(repo.parent / 'x.py'),
                           '--json', capsys=capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_ERROR
    assert payload['error'] == 'REPOSITORY_ERROR'
    assert payload['unaccounted_paths'] == []
