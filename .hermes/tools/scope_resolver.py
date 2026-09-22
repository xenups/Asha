#!/usr/bin/env python3
"""Deterministic semantic blast-radius scope resolver (S0-S4).

Zero-daemon, stdlib-only. Inspects a git diff (base..worktree by default),
classifies every changed path, and returns ``max(detected_scopes)`` under the
strict ordering S4 > S3 > S2 > S1 > S0.

Levels:
    S4  root/toolchain configuration (pyproject, ruff/mypy config, lockfiles,
        .github/**, pre-commit, python version pins)
    S3  contract drift: public signatures, exported types, class schemas,
        added/removed public API -- OR any semantic ambiguity (parse errors,
        dynamic call-sites of changed private helpers). No down-scoping on
        ambiguity: fail-closed, status "uncertain".
    S2  internal package runtime logic without contract changes
    S1  private/local helpers whose changed names have zero call-sites
        (trace_impact == 0) and no public export/schema changes
    S0  non-runtime: docs/markdown/rst, tests/**, LICENSE

Output is structured JSON (see resolve()) with evaluated scope, affected
files, per-file classification, status and the mandatory check matrix.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

LEVELS = ('S0', 'S1', 'S2', 'S3', 'S4')
LEVEL_RANK = {level: rank for rank, level in enumerate(LEVELS)}

S4_NAMES = {
    'pyproject.toml', 'ruff.toml', 'mypy.ini', '.mypy.ini', 'setup.cfg',
    'tox.ini', '.python-version', '.tool-versions', 'poetry.lock',
    'uv.lock', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
    '.pre-commit-config.yaml', 'ruff.toml.j2',
}
S4_DIRS = ('.github', '.circleci', '.vscode')
S4_SUFFIXES = ('.lock', '.nix')
S0_EXTS = ('.md', '.rst', '.mdx')
S0_DIRS = ('tests/', 'test/', 'docs/', 'doc/', 'examples/', 'benchmarks/')
S0_NAMES = {'license', 'license.txt', 'license.md', 'changelog.md', 'makefile.doc'}
PYTHON_SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__',
                    '.mypy_cache', '.ruff_cache', '.pytest_cache', 'dist',
                    'build', 'site-packages'}

# Fail-closed dynamic constructs: unknown call-sites / dynamic imports.
_DYNAMIC_RE = re.compile(
    r'\beval\s*\(|\bexec\s*\(|\bglobals\s*\(|\blocals\s*\(|'
    r'__import__\s*\(|importlib\b'
)

# Mandatory check matrix per scope level.
SCOPE_CHECKS: dict[str, list[str]] = {
    'S0': ['pytest'],
    'S1': ['pytest'],
    'S2': ['ruff', 'pytest', 'mypy'],
    'S3': ['ruff', 'pytest', 'mypy'],
    'S4': ['ruff', 'pytest', 'mypy'],
}


class ScopeError(Exception):
    """Git/parse failure while resolving scope (fail-closed)."""


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(['git', *args], cwd=root, capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        raise ScopeError('git ' + ' '.join(args) + ': ' + proc.stderr.strip())
    return proc.stdout


def _try_git(root: Path, *args: str) -> str | None:
    proc = subprocess.run(['git', *args], cwd=root, capture_output=True,
                          text=True, timeout=60)
    return proc.stdout if proc.returncode == 0 else None


def default_base(root: Path) -> str | None:
    """origin/main when it exists, else HEAD~1, else None (worktree only)."""
    if _try_git(root, 'rev-parse', '--verify', 'origin/main') is not None:
        return 'origin/main'
    if _try_git(root, 'rev-parse', '--verify', 'HEAD~1') is not None:
        return 'HEAD~1'
    return None


def changed_files(root: Path, base: str | None) -> list[str]:
    names: set[str] = set()
    target = f'{base}' if base else 'HEAD'
    proc = subprocess.run(['git', 'diff', '--name-only', target], cwd=root,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode == 0:
        names.update(line for line in proc.stdout.splitlines() if line.strip())
    else:
        raise ScopeError('git diff failed: ' + proc.stderr.strip())
    untracked = _try_git(root, 'ls-files', '--others', '--exclude-standard')
    if untracked:
        names.update(line for line in untracked.splitlines() if line.strip())
    return sorted(names)


def classify_path(path: str) -> str | None:
    """Static path-based classification; None = needs semantic analysis."""
    posix = path.replace('\\', '/')
    name = posix.rsplit('/', 1)[-1].lower()
    if posix.startswith(S4_DIRS) or name in S4_NAMES or posix.endswith(S4_SUFFIXES):
        return 'S4'
    if name in S0_NAMES or posix.lower().endswith(S0_EXTS) or \
            posix.startswith(S0_DIRS) or posix.endswith('/' + 'tests'):
        return 'S0'
    return None


def _surface(source: str) -> dict[str, str]:
    """Public API surface: public funcs/methods signatures + class schemas."""
    tree = ast.parse(source)
    surface: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith('_'):
                sig = ast.unparse(node.args) + '->' + (
                    ast.unparse(node.returns) if node.returns else 'None')
                surface['def:' + node.name] = sig
        elif isinstance(node, ast.ClassDef):
            bases = ','.join(ast.unparse(b) for b in node.bases)
            schema: list[str] = ['bases=' + bases]
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    schema.append(item.target.id + ':' +
                                  (ast.unparse(item.annotation) if item.annotation else ''))
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and not item.name.startswith('_'):
                    sig = ast.unparse(item.args) + '->' + (
                        ast.unparse(item.returns) if item.returns else 'None')
                    schema.append('method:' + item.name + '=' + sig)
            surface['class:' + node.name] = ';'.join(schema)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == '__all__':
                    surface['__all__'] = ast.unparse(node.value)
    return surface


def _private_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name.startswith('_'):
            names.add(node.name)
    return names


def _def_ranges(source: str) -> list[tuple[int, int, str]]:
    tree = ast.parse(source)
    ranges: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, 'end_lineno', None) or start
            ranges.append((start, end, node.name))
    return ranges


def _enclosing(ranges: list[tuple[int, int, str]], start: int, end: int) -> set[str]:
    return {name for s, e, name in ranges if e >= start and s <= end}


def _changed_enclosers(base_src: str | None, new_src: str | None) -> set[str]:
    """Names of defs/classes whose lines were added, removed or modified."""
    names: set[str] = set()
    matcher = difflib.SequenceMatcher(
        None, (base_src or '').splitlines(), (new_src or '').splitlines())
    try:
        new_ranges = _def_ranges(new_src) if new_src else []
        base_ranges = _def_ranges(base_src) if base_src else []
    except SyntaxError:
        return names
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue
        if j1 < j2 and new_ranges:
            names |= _enclosing(new_ranges, j1 + 1, j2)
        if i1 < i2 and base_ranges:
            names |= _enclosing(base_ranges, i1 + 1, i2)
    return names


def _callers(root: Path, name: str) -> int:
    """trace_impact: count call-sites of `name` across the repository."""
    count = 0
    for path in root.rglob('*.py'):
        if any(part in PYTHON_SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue  # ambiguity is handled upstream by the caller
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Name) and func.id == name) or \
                        (isinstance(func, ast.Attribute) and func.attr == name):
                    count += 1
    return count


def _dynamic_reference(root: Path, name: str) -> bool:
    """Unknown call-site: name appears as a string literal (getattr/registry)."""
    needle = json.dumps(name)[1:-1]  # name without quotes, escapes kept
    for path in root.rglob('*.py'):
        if any(part in PYTHON_SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(r'["\']' + re.escape(needle) + r'["\']', text):
            return True
    return False


def _base_content(root: Path, base: str | None, path: str) -> str | None:
    if base is None:
        return None
    proc = subprocess.run(['git', 'show', f'{base}:{path}'], cwd=root,
                          capture_output=True, text=True, timeout=60)
    return proc.stdout if proc.returncode == 0 else None


def _classify_python(root: Path, base: str | None, path: str,
                     reasons: list[str]) -> tuple[str, bool]:
    """-> (scope, uncertain)."""
    full = root / path
    new_src = full.read_text(encoding='utf-8') if full.is_file() else None
    base_src = _base_content(root, base, path)

    for label, src in (('new', new_src), ('base', base_src)):
        if src is None:
            continue
        try:
            ast.parse(src)
        except SyntaxError as exc:
            reasons.append(f'{path}: unparseable {label} source ({exc.msg})')
            return 'S3', True

    try:
        base_surface = _surface(base_src) if base_src else {}
        new_surface = _surface(new_src) if new_src else {}
    except SyntaxError:
        return 'S3', True

    # Comment-only (or whitespace) edit: AST identical -> non-runtime S0.
    if base_src and new_src and \
            ast.dump(ast.parse(base_src)) == ast.dump(ast.parse(new_src)):
        reasons.append(f'{path}: AST identical (comment-only edit) -> S0')
        return 'S0', False

    if base_surface != new_surface:
        reasons.append(f'{path}: public API surface changed')
        return 'S3', False

    enclosers = _changed_enclosers(base_src, new_src)
    private_changed = {n for n in enclosers if n.startswith('_')}
    public_changed = enclosers - private_changed

    if public_changed:
        reasons.append(f'{path}: runtime logic inside '
                       f'{sorted(public_changed)} (contract unchanged)')
        return 'S2', False

    if private_changed:
        probe = (new_src or '') + (base_src or '')
        for name in private_changed:
            if _DYNAMIC_RE.search(probe) or _dynamic_reference(root, name):
                reasons.append(
                    f'{path}: ambiguous dynamic call-site of {name}')
                return 'S3', True
        zero_impact = all(_callers(root, name) == 0
                          for name in private_changed)
        if zero_impact:
            reasons.append(f'{path}: private {sorted(private_changed)} '
                           'with 0 callers -> S1')
            return 'S1', False
        reasons.append(f'{path}: private {sorted(private_changed)} '
                       'has callers -> S2')
        return 'S2', False

    if new_src is None and base_src is not None:
        reasons.append(f'{path}: deleted without public surface delta')
        return 'S2', False
    reasons.append(f'{path}: module-level runtime logic changed')
    return 'S2', False


def resolve(root: Path | str, base: str | None = None) -> dict:
    root = Path(root).resolve()
    base = base if base is not None else default_base(root)
    paths = changed_files(root, base)
    per_file: dict[str, str] = {}
    reasons: list[str] = []
    uncertain = False

    for path in paths:
        static = classify_path(path)
        if static is not None:
            per_file[path] = static
            reasons.append(f'{path}: static rule -> {static}')
            continue
        if path.lower().endswith(('.py', '.pyi')):
            scope, flag = _classify_python(root, base, path, reasons)
        else:
            scope, flag = 'S2', False
            reasons.append(f'{path}: non-python runtime file -> S2')
        per_file[path] = scope
        uncertain = uncertain or flag

    if not paths:
        scope = 'S0'
        reasons.append('no changes detected')
    else:
        scope = max(per_file.values(), key=lambda level: LEVEL_RANK[level])

    # Fail-closed: ambiguity may only elevate, never downgrade.
    status = 'uncertain' if uncertain else 'certain'
    if uncertain and LEVEL_RANK[scope] < LEVEL_RANK['S3']:
        reasons.append('ambiguity elevated scope to S3 (no down-scoping)')
        scope = 'S3'

    return {
        'scope': scope,
        'status': status,
        'base': base,
        'affected_files': paths,
        'per_file': per_file,
        'reasons': reasons,
        'checks': SCOPE_CHECKS[scope],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.', help='repository root')
    parser.add_argument('--base', default=None,
                        help='diff base ref (default: origin/main or HEAD~1)')
    parser.add_argument('--json', action='store_true', help='print full JSON')
    args = parser.parse_args(argv)
    try:
        result = resolve(args.root, args.base)
    except ScopeError as exc:
        print('SCOPE ERROR: ' + str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"scope={result['scope']} status={result['status']} "
              f"files={len(result['affected_files'])}")
        for reason in result['reasons']:
            print('  ' + reason)
    return 0


if __name__ == '__main__':
    sys.exit(main())
