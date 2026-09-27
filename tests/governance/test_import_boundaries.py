"""Phase I -- Governance/AST import-boundary regression guard.

Locks the Layer-1 invariant established by H.3 and H-AST:

    Governance Core MUST NOT be blocked by AST/CodeGraph tooling.

Guarded pure-governance modules:

    asha/governance/evaluator.py
    asha/governance/ship_gate.py
    asha/evidence.py
    asha/scope_resolver.py

Rejected dependency targets (the Layer-3 authoring toolkit):

    asha.ast_indexer
    asha.codegraph
    asha.context_slicer

Each pure governance module above must contain NO statically detectable
import (direct, from-, aliased, relative, or dynamic via
importlib.import_module / __import__) of the toolkit.

asha/scoping.py is the DOCUMENTED EXCEPTION, classified SOFT_HINT by
H.3: it uses ast_indexer/codegraph as optional, fail-closed enrichment
(every graph/index failure degrades to a COMPLETE decision with an
explicit F_GRAPH_FAILURE reason -- never a crash, never a guessed
subset). Its guard therefore verifies the SOFT_HINT boundary: imports
restricted to the two known enrichment symbols, and the fail-closed
wrapper present -- NOT absence of the imports.

This is a STATIC check. It does NOT claim to prove absence of
arbitrary runtime dynamic imports; its contract is:

    No statically detectable Governance -> AST/CodeGraph dependency
    (outside the documented scoping SOFT_HINT boundary).

No production code may be modified to satisfy this test.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

PURE_GOVERNANCE_MODULES = [
    "asha/governance/evaluator.py",
    "asha/governance/ship_gate.py",
    "asha/evidence.py",
    "asha/scope_resolver.py",
]

SCOPING_MODULE = "asha/scoping.py"

# known scoping SOFT_HINT enrichment symbols (H.3)
SCOPING_ALLOWED_IMPORTS = {
    "asha.ast_indexer": {"index_module"},
    "asha.codegraph": {"build_graph"},
}

FORBIDDEN = {
    "asha.ast_indexer",
    "asha.codegraph",
    "asha.context_slicer",
}

# dynamic-import call forms detected statically:
#   importlib.import_module("asha.codegraph")
#   __import__("asha.codegraph", ...)
_DYNAMIC_CALL_PATTERN = re.compile(
    r'(?:importlib\s*\.\s*import_module|__import__)\s*\('
    r'["\'](asha\.(?:ast_indexer|codegraph|context_slicer))["\']'
)


def _module_name(node: ast.AST) -> str | None:
    """Recover the dotted module name from an import statement,
    resolving relative imports against the asha package root so
    `.ast_indexer` == `asha.ast_indexer`."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            return alias.name
    if isinstance(node, ast.ImportFrom):
        if node.module and node.level and node.level > 0:
            # level 1 = current package (asha) for asha-internal
            # modules; deeper levels are out of scope here
            return "asha." + node.module if node.level == 1 \
                else node.module
        if node.module:
            return node.module
    return None


def _scan_imports(source: str) -> list[tuple[int, str, str | None]]:
    """[(lineno, module, imported_name_or_None)] for import nodes."""
    found: list[tuple[int, str, str | None]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name, None))
        elif isinstance(node, ast.ImportFrom):
            name = _module_name(node)
            if name is not None:
                for alias in node.names:
                    found.append((node.lineno, name, alias.name))
    return found


def _scan_dynamic_strings(source: str) -> list[tuple[str, int]]:
    """[(module, line)] for importlib/__import__ string forms."""
    hits: list[tuple[str, int]] = []
    for m in _DYNAMIC_CALL_PATTERN.finditer(source):
        line = source.count(chr(10), 0, m.start()) + 1
        hits.append((m.group(1), line))
    return hits


@pytest.mark.parametrize("rel_path", PURE_GOVERNANCE_MODULES)
def test_governance_core_has_no_ast_dependency(rel_path: str) -> None:
    path = REPO_ROOT / rel_path
    assert path.is_file(), f"guard file missing: {rel_path}"
    source = path.read_text(encoding="utf-8")
    offenders: list[tuple[str, str]] = [
        (str(ln), mod) for ln, mod, _ in _scan_imports(source)
        if mod in FORBIDDEN
    ]
    offenders += [("(dynamic)", mod) for mod, _ in
                  _scan_dynamic_strings(source)]
    assert not offenders, (
        f"{rel_path} violates the Governance/AST boundary: "
        f"{sorted(set(offenders))}"
    )


def test_scoping_soft_hint_boundary() -> None:
    """scoping.py (H.3 SOFT_HINT): AST imports restricted to the two
    known enrichment symbols AND the fail-closed wrapper present."""
    path = REPO_ROOT / SCOPING_MODULE
    assert path.is_file(), f"guard file missing: {SCOPING_MODULE}"
    source = path.read_text(encoding="utf-8")

    ast_imports = [(mod, name) for ln, mod, name in _scan_imports(source)
                   if mod in FORBIDDEN]
    assert ast_imports, (
        "scoping.py lost its enrichment imports; update the boundary "
        "test before changing scoping behavior"
    )
    for mod, name in ast_imports:
        assert name in SCOPING_ALLOWED_IMPORTS.get(mod, ()), (
            f"scoping.py imports unexpected AST symbol "
            f"{mod}.{name} -- SOFT_HINT boundary violated"
        )
    # context_slicer is packaging-only and must never enter governance
    assert not any(mod == "asha.context_slicer"
                   for mod, _ in ast_imports), (
        "context_slicer is packaging-only and must not enter scoping"
    )
    # fail-closed wrapper present (H.3 invariant)
    assert "F_GRAPH_FAILURE" in source, (
        "scoping.py must keep its fail-closed GRAPH_FAILURE decision"
    )
    assert "except Exception" in source, (
        "scoping.py must keep its fail-closed exception wrapper"
    )
    assert not _scan_dynamic_strings(source), (
        "scoping.py must not use dynamic import string forms"
    )


def test_no_string_dynamic_import_forms_in_pure_governance() -> None:
    """Catch string-literal dynamic forms missed by the AST walk."""
    for rel_path in PURE_GOVERNANCE_MODULES:
        source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for mod, line in _scan_dynamic_strings(source):
            raise AssertionError(
                f"{rel_path} contains dynamic import of {mod} "
                f"(line {line})"
            )