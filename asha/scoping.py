"""Phase 5.0 -- Risk-Aware Scoped Validation eligibility engine.

Pure evaluator / completeness prover: the SOLE runtime authority that
decides whether a worker's validation may run SCOPED. Contract
(docs/phase-5.0-design.md + Step 2 spec):

* Fail-closed: any unprovable condition -> COMPLETE with a stable,
  machine-readable ``fallback_reason`` (never prose, never a guess).
* Exhaustive: reverse closure is a visited-set BFS with no depth limit;
  parser/index/coverage mismatches invalidate the proof.
* Zero poison boundaries: UNRESOLVED/EXTERNAL targets (outside
  stdlib/builtin) or dynamic mechanisms anywhere in the proof footprint
  (changed modules + their reverse dependents) force COMPLETE.
* ``check_runner.run_scoped`` is a dumb executor of this decision; only
  the scheduler may call it, and only with an affirmative decision.

Determinism: every input is (repo state at root, changed paths,
classification, scope level/status) -- no clock, no environment, no
randomness, no heuristics.
"""
from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ast_indexer import index_module
from .codegraph import build_graph
from .scope_resolver import PYTHON_SKIP_DIRS

SCOPED = 'SCOPED'
COMPLETE = 'COMPLETE'

# Stable machine-readable fallback vocabulary (Step 2 spec section 3A).
F_INVALID_ENVELOPE = 'INVALID_ENVELOPE'
F_UNKNOWN_CLASSIFICATION = 'UNKNOWN_CLASSIFICATION'
F_PROVEN_SHARED = 'PROVEN_SHARED'
F_FORBIDDEN_SCOPE_LEVEL = 'FORBIDDEN_SCOPE_LEVEL'
F_SCOPE_STATUS_UNCERTAIN = 'SCOPE_STATUS_UNCERTAIN'
F_NO_CHANGED_FILES = 'NO_CHANGED_FILES'
F_PYTEST_CLOSURE_UNPROVEN = 'PYTEST_CLOSURE_UNPROVEN'
F_UNRESOLVED_BOUNDARY = 'UNRESOLVED_BOUNDARY'
F_EXTERNAL_BOUNDARY = 'EXTERNAL_BOUNDARY'
F_DYNAMIC_IMPORT = 'DYNAMIC_IMPORT'
F_DYNAMIC_GETATTR = 'DYNAMIC_GETATTR'
F_INCOMPLETE_CLOSURE = 'INCOMPLETE_CLOSURE'
F_GRAPH_FAILURE = 'GRAPH_FAILURE'
F_MYPY_CLOSURE_UNPROVEN = 'MYPY_CLOSURE_UNPROVEN'
F_ELIGIBILITY_EXCEPTION = 'ELIGIBILITY_EXCEPTION'
F_INVALID_DECISION = 'INVALID_DECISION'

ALLOWED_SCOPE_LEVELS = frozenset({'S0', 'S1', 'S2'})
_CLASSIFICATION_DISJOINT = 'PROVEN_DISJOINT'
_CLASSIFICATION_SHARED = 'PROVEN_SHARED'
_CLASSIFICATION_UNKNOWN = 'UNKNOWN'

# Dynamic constructs that hide dependency edges entirely (Step 2 spec
# section 2.1). eval/exec/__import__/import_module are also flagged by
# the AST indexer's dynamic markers; this walk adds globals/locals,
# any importlib attribute call, and non-literal getattr.
_DYNAMIC_NAMES = frozenset({'eval', 'exec', '__import__', 'globals',
                            'locals'})
# PYTHON_SKIP_DIRS (import machines) plus repo-local non-source trees:
# .hermes (venv) and benchmarks/results/ (gitignored benchmark fixture
# output -- thousands of throwaway .py files that are never part of a
# governed change set; a change there is unindexable and falls back to
# PYTEST_CLOSURE_UNPROVEN). A top-level 'results/' dir anywhere is
# treated the same way (documented ceiling: no governed package may
# house source under a component literally named 'results').
_SKIP_DIRS = frozenset(PYTHON_SKIP_DIRS) | {
    '.hermes', 'results', '.git', '.mypy_cache',
}
_STDLIB_ROOTS = frozenset(sys.stdlib_module_names)


@dataclass(frozen=True)
class ScopingDecision:
    """Immutable verdict of the Eligibility Engine.

    Eligible: ``mode == SCOPED``, empty ``fallback_reason``, explicit
    target tuples. Ineligible: ``mode == COMPLETE`` with a mandatory
    machine-readable ``fallback_reason``.
    """

    eligible: bool
    mode: str
    targeted_tests: tuple[str, ...] = ()
    mypy_targets: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()
    fallback_reason: str = ''

    def evidence_metadata(self) -> dict[str, Any]:
        """Worker-envelope scoping metadata (Step 2 spec section 2.4).

        Exactly the five sanctioned keys: validation_mode always;
        targets + omission_rationale only for SCOPED; fallback_reason
        only for COMPLETE.
        """
        if self.eligible:
            return {
                'validation_mode': SCOPED,
                'targeted_tests': list(self.targeted_tests),
                'mypy_targets': list(self.mypy_targets),
                'omission_rationale': ';'.join(self.rationale),
            }
        return {
            'validation_mode': COMPLETE,
            'fallback_reason': self.fallback_reason,
        }


def complete_decision(reason: str,
                      *rationale: str) -> ScopingDecision:
    """Constructor for a COMPLETE fallback (stable reason vocabulary)."""
    return ScopingDecision(eligible=False, mode=COMPLETE,
                           rationale=tuple(rationale),
                           fallback_reason=reason)


def assess_scoping_eligibility(
        repo_root: Path,
        changed_files: list[str],
        task_classification: str | None,
        scope_level: str,
        *,
        scope_status: str = 'certain',
        envelope_valid: bool = True,
) -> ScopingDecision:
    """Evaluate SCOPED eligibility for one change set (fail-closed).

    Order of checks is fixed and first-failure-wins, so the decision is
    deterministic for identical inputs. ANY unexpected exception is
    converted to COMPLETE (ELIGIBILITY_EXCEPTION) -- never to SCOPED.
    """
    try:
        return _assess(Path(repo_root), list(changed_files),
                       task_classification, scope_level,
                       scope_status, envelope_valid)
    except Exception as exc:  # fail-closed boundary (spec 1.2)
        return complete_decision(
            F_ELIGIBILITY_EXCEPTION,
            f'{type(exc).__name__}: {str(exc)[:200]}')


def _assess(root: Path, changed: list[str], classification: str | None,
            scope_level: str, scope_status: str,
            envelope_valid: bool) -> ScopingDecision:
    # 1. classification/envelope validity
    if not envelope_valid:
        return complete_decision(F_INVALID_ENVELOPE)
    if classification is None or classification != _CLASSIFICATION_DISJOINT:
        if classification == _CLASSIFICATION_SHARED:
            return complete_decision(F_PROVEN_SHARED)
        return complete_decision(F_UNKNOWN_CLASSIFICATION,
                                 f'classification={classification!r}')

    # 2. strict S-level boundary (S3/S4 hard lockout)
    if scope_level not in ALLOWED_SCOPE_LEVELS:
        return complete_decision(F_FORBIDDEN_SCOPE_LEVEL,
                                 f'scope_level={scope_level!r}')

    # 3. scope capture must be certain (ambiguity may only elevate)
    if scope_status != 'certain':
        return complete_decision(F_SCOPE_STATUS_UNCERTAIN,
                                 f'scope_status={scope_status!r}')

    # 4. a non-empty change set is required to scope anything
    if not changed:
        return complete_decision(F_NO_CHANGED_FILES)

    # 5. build the graph (parser/index failures invalidate the proof)
    try:
        sources, texts, module_of_rel, rel_of_module = _index_repository(root)
        graph = build_graph(tuple(sources.values()))
    except Exception as exc:
        return complete_decision(
            F_GRAPH_FAILURE, f'{type(exc).__name__}: {str(exc)[:200]}')

    # 6. coverage completeness: every repo .py must be indexed
    filesystem_files = _repository_py_files(root)
    indexed_rels = set(module_of_rel)
    if indexed_rels != filesystem_files:
        missing = sorted(filesystem_files - indexed_rels)
        extra = sorted(indexed_rels - filesystem_files)
        return complete_decision(
            F_INCOMPLETE_CLOSURE,
            f'index_coverage_mismatch missing={missing[:5]}'
            f' extra={extra[:5]}')

    # 7. changed files must be attributable through the code graph
    changed_py = [entry for entry in changed
                  if entry.endswith(('.py', '.pyi'))]
    if len(changed_py) != len(changed):
        unattributable = [entry for entry in changed
                          if not entry.endswith(('.py', '.pyi'))]
        return complete_decision(
            F_PYTEST_CLOSURE_UNPROVEN,
            f'changed file outside code graph: {unattributable[:5]}')
    missing_changed = [entry for entry in changed_py
                       if entry not in module_of_rel]
    if missing_changed:
        return complete_decision(
            F_PYTEST_CLOSURE_UNPROVEN,
            f'changed file not indexable: {missing_changed[:5]}')
    conftest = [entry for entry in changed_py
                if Path(entry).name == 'conftest.py']
    if conftest:
        return complete_decision(
            F_PYTEST_CLOSURE_UNPROVEN,
            f'conftest changed: {conftest[:3]}')

    # 8. exhaustive reverse closure over consumers (visited-set BFS,
    #    no depth limit, no truncation by construction)
    seeds: list[str] = []
    for entry in changed_py:
        module = module_of_rel[entry]
        seeds.append(f'mod:{module}')
        index = sources[module]
        seeds.extend(f'sym:{module}:{fact.name}' for fact in index.symbols)
    visited = _reverse_closure(graph, tuple(seeds))
    footprint_modules = _footprint_modules(visited, rel_of_module)

    # 9. poison boundary scan over the proof footprint
    poison = _scan_poison(graph, sorted(footprint_modules),
                          texts, rel_of_module)
    if poison is not None:
        return poison

    # 10. derive targets: deterministic, sorted, conservative
    targeted = {
        rel_of_module[module] for module in footprint_modules
        if rel_of_module[module].startswith('tests/')
    }
    targeted |= {
        entry for entry in changed_py if entry.startswith('tests/')
    }
    targeted_tests = tuple(sorted(targeted))
    # A non-empty selection whose files contain no pytest-collectable
    # test function would make the authorized run contribute zero
    # observations; refuse to dress that up as SCOPED (fail-closed:
    # fall back to COMPLETE rather than emit a skipped pytest entry).
    # An EMPTY set stays eligible: spec 3B explicitly allows a proven
    # empty target set.
    if targeted_tests and not any(
            _has_test_function(texts[module_of_rel[rel]])
            for rel in targeted_tests if rel in module_of_rel):
        return complete_decision(
            F_PYTEST_CLOSURE_UNPROVEN,
            f'no test function in targeted set: '
            f'{list(targeted_tests)[:5]}')
    mypy_targets = tuple(sorted(set(changed_py) | {
        rel_of_module[module] for module in footprint_modules
    }))
    if not mypy_targets:
        return complete_decision(F_MYPY_CLOSURE_UNPROVEN,
                                 'no typed target derivable')

    rationale = (
        f'indexed_modules={len(sources)}',
        f'footprint_modules={len(footprint_modules)}',
        f'reverse_sources={len(visited)}',
        f'targeted_tests={len(targeted_tests)}',
        'complete_reverse_closure_proof',
    )
    return ScopingDecision(
        eligible=True, mode=SCOPED,
        targeted_tests=targeted_tests,
        mypy_targets=mypy_targets,
        changed_files=tuple(changed),
        rationale=rationale,
        fallback_reason='')


def _repository_py_files(root: Path) -> set[str]:
    """Every repository .py/.pyi the index is required to cover.

    Uses a pruning walk (skip dirs are never descended into) instead of
    rglob+filter: walking the venv tree first cost seconds per call.
    """
    found: set[str] = set()
    for base, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for filename in filenames:
            if not filename.endswith(('.py', '.pyi')):
                continue
            found.add((Path(base) / filename).relative_to(root).as_posix())
    return found


def _index_repository(
        root: Path,
) -> tuple[dict[str, Any], dict[str, str], dict[str, str], dict[str, str]]:
    """Index every repo .py -> (sources by module, texts by module,
    module_by_rel, rel_by_module). Raises ValueError on unparseable
    source (fail-closed -- the proof must not silently skip files)."""
    sources: dict[str, Any] = {}
    texts: dict[str, str] = {}
    module_of_rel: dict[str, str] = {}
    rel_of_module: dict[str, str] = {}
    for rel in sorted(_repository_py_files(root)):
        source = (root / rel).read_text(encoding='utf-8',
                                        errors='replace')
        module = _module_name(rel)
        index = index_module(module, source)
        sources[module] = index
        texts[module] = source
        module_of_rel[rel] = module
        rel_of_module[module] = rel
    return sources, texts, module_of_rel, rel_of_module


def _module_name(rel: str) -> str:
    """'pkg/mod.py' -> 'pkg.mod'; 'pkg/__init__.py' -> 'pkg'."""
    stem = rel[:-3] if rel.endswith('.py') else rel[:-4]
    parts = stem.split('/')
    if parts[-1] == '__init__':
        parts = parts[:-1]
    if not parts:
        raise ValueError(f'scoping: cannot derive module for {rel!r}')
    return '.'.join(parts)


def _reverse_closure(graph: Any, roots: tuple[str, ...]) -> set[str]:
    """Exhaustive reverse BFS (consumer side): visited-set, no depth
    limit, deterministic over the sorted edge tuple."""
    incoming: dict[str, list[str]] = {}
    for edge in graph.edges:
        incoming.setdefault(edge.target, []).append(edge.source)
    visited: set[str] = set()
    queue = list(roots)
    while queue:
        node = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)
        for source in sorted(incoming.get(node, ())):
            if source not in visited:
                queue.append(source)
    return visited


def _footprint_modules(visited: set[str],
                       rel_of_module: dict[str, str]) -> set[str]:
    """Modules whose facts participate in the proof: every visited
    consumer plus the module owning each visited node."""
    modules: set[str] = set()
    for node in visited:
        if node.startswith('mod:'):
            modules.add(node[4:])
        elif node.startswith('sym:'):
            rest = node[4:]
            module = rest.rsplit(':', 1)[0]
            if module in rel_of_module:
                modules.add(module)
    return modules


def _scan_poison(graph: Any, footprint: list[str],
                 texts: dict[str, str],
                 rel_of_module: dict[str, str]
                 ) -> ScopingDecision | None:
    """Zero poison boundaries in the proof footprint.

    Scans EVERY edge emitted by a footprint module -- module-level
    import edges and symbol-level name edges alike. Per module
    (sorted): dynamic constructs first (they hide edges entirely),
    then UNRESOLVED targets, then non-stdlib/non-builtin EXTERNAL
    targets. Builtins and stdlib imports are deterministically
    resolvable and never poison.
    """
    footprint_set = set(footprint)
    module_node_targets: dict[str, list[str]] = {}
    for edge in graph.edges:
        source = edge.source
        owner: str | None = None
        if source.startswith('mod:'):
            body = source[4:]
            if body in footprint_set:
                owner = body
        elif source.startswith('sym:'):
            body = source[4:]
            candidate = body.split(':', 1)[0]
            if candidate in footprint_set:
                owner = candidate
        if owner is not None:
            module_node_targets.setdefault(owner, []).append(edge.target)
    for module in footprint:
        rel = rel_of_module[module]
        dynamic = _scan_dynamic(texts.get(module, ''), rel)
        if dynamic is not None:
            return dynamic
        for target in sorted(set(module_node_targets.get(module, ()))):
            if target.startswith('unk:'):
                if target.startswith('unk:dynamic_import:'):
                    return complete_decision(
                        F_DYNAMIC_IMPORT, f'{rel} -> {target}')
                return complete_decision(
                    F_UNRESOLVED_BOUNDARY, f'{rel} -> {target}')
            if target.startswith('ext:'):
                root_name = target[4:].split('.')[0]
                if root_name not in _STDLIB_ROOTS:
                    return complete_decision(
                        F_EXTERNAL_BOUNDARY, f'{rel} -> {target}')
    return None


def _scan_dynamic(source: str, rel: str) -> ScopingDecision | None:
    if not source:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return complete_decision(
            F_GRAPH_FAILURE, f'unparseable {rel}: {exc.msg}')
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id == 'getattr':
                if len(node.args) >= 2 and not isinstance(
                        node.args[1], ast.Constant):
                    return complete_decision(
                        F_DYNAMIC_GETATTR, f'{rel} -> computed getattr')
            elif func.id in _DYNAMIC_NAMES:
                return complete_decision(
                    F_DYNAMIC_IMPORT, f'{rel} -> {func.id}()')
        elif isinstance(func, ast.Attribute):
            root_name = _dotted(func.value)
            if root_name and root_name.split('.')[0] == 'importlib':
                return complete_decision(
                    F_DYNAMIC_IMPORT, f'{rel} -> importlib call')
    return None


def _has_test_function(source: str) -> bool:
    """pytest-collectable proof: module defines def test_* / class Test*."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name.startswith('test_'):
            return True
        if isinstance(node, ast.ClassDef) and node.name.startswith('Test'):
            return True
    return False


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f'{base}.{node.attr}' if base else None
    return None
