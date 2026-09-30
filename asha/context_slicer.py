"""Phase 4.1 -- surgical context extraction: target body + minimum
SOUND dependency representations.

Contract: the TARGET keeps its full source verbatim; each dependency
gets a minimal stub that preserves name, signature, parameter/return
types, generic parameters, base classes, class-level constants and
TypeAlias/Protocol structure (§stub-safety). A slice is an AGENT
CONTEXT, not an executable program -- boundary dependencies (external,
builtin, UNRESOLVED) are represented by explicit marker lines and are
NEVER dropped: shrinking context must never shrink the truth the agent
needs.

all inputs are explicit (graph + indices + target source); slicing
touches no filesystem, no child processes, no clock, no environment.
Determinism: stubs sorted by node id; identical inputs -> identical
bytes.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

from .ast_indexer import ModuleIndex, SymbolFacts
from .codegraph import ClosureResult, CodeGraph, closure


@dataclass(frozen=True)
class DependencyStub:
    node: str
    text: str


@dataclass(frozen=True)
class ContextSlice:
    target_name: str
    target_source: str
    stubs: tuple[DependencyStub, ...]
    closure: ClosureResult
    full_bytes: int        # target + FULL sources of every dependency
    surgical_bytes: int     # target + stubs
    symbol_count: int       # resolved symbols whose source was inlined
    completeness: str = "UNKNOWN"   # COMPLETE | INCOMPLETE | UNKNOWN
    completeness_reasons: tuple[str, ...] = ()


def slice_context(target_source: str, *, target_name: str,
                  target_module: str, graph: CodeGraph,
                  indices: tuple[ModuleIndex, ...],
                  reverse: bool = False,
                  max_nodes: int | None = None) -> ContextSlice:
    root = f'sym:{target_module}:{target_name}'
    reach = closure(graph, (root,), target_module, reverse=reverse,
                    max_nodes=max_nodes)
    by_node = _node_index(indices)

    stubs: list[DependencyStub] = []
    full_parts = [target_source]      # emitted context: compact stubs
    full_sources = [target_source]    # full baseline: real sources
    symbol_count = 0
    for node in reach.reachable:
        if node == root:
            continue
        if node.startswith('sym:'):
            fact = by_node.get(node)
            if fact is None:
                text = f'# missing symbol index: {node}'
            else:
                text = stub_for(fact)
                full_sources.append(fact.source)
            symbol_count += 1
            full_parts.append(text)
        elif node.startswith('mod:'):
            text = f'# module dependency: {node[4:]}'
        elif node.startswith('ext:'):
            text = f'# external dependency: {node[4:]}'
        elif node.startswith('builtin:'):
            text = f'# builtin: {node[8:]}'
        else:
            text = f'# UNRESOLVED dependency: {node}'
        full_parts.append(text)
        full_sources.append(text)
        stubs.append(DependencyStub(node=node, text=text))

    stubs.sort(key=lambda stub: stub.node)
    # full baseline: real full sources for resolved symbols, boundary
    # markers otherwise (fair comparison -- boundaries exist in both).
    # EMITTED context (full_parts) stays stub-compact (K.6.1 R2).
    full_bytes = len(''.join(full_sources).encode('utf-8'))
    surgical_bytes = len(
        (target_source + ''.join(
            stub.text for stub in stubs)).encode('utf-8'))
    reasons: list[str] = []
    if reach.budget_exhausted:
        state = "INCOMPLETE"
        reasons.append(f"expansion budget {max_nodes} exhausted")
    elif reach.unresolved:
        state = "INCOMPLETE"
        reasons.append(f"{len(reach.unresolved)} unresolved node(s) "
                       f"did not expand")
    elif symbol_count == 0 and not stubs:
        state = "INCOMPLETE"
        reasons.append("no dependencies resolved")
    else:
        state = "COMPLETE"
    return ContextSlice(
        target_name=target_name,
        target_source=target_source,
        stubs=tuple(stubs),
        closure=reach,
        full_bytes=full_bytes,
        surgical_bytes=surgical_bytes,
        symbol_count=symbol_count,
        completeness=state,
        completeness_reasons=tuple(reasons),
    )


def _node_index(indices: tuple[ModuleIndex, ...]
                ) -> dict[str, SymbolFacts]:
    table: dict[str, SymbolFacts] = {}
    for index in indices:
        for fact in index.symbols:
            table[f'sym:{index.module}:{fact.name}'] = fact
    return table


def stub_for(fact: SymbolFacts) -> str:
    """Minimal sound representation of a dependency symbol.

    context slice != executable program (by design).
    """
    try:
        tree = ast.parse(fact.source)
    except SyntaxError:
        return f'# unparseable dependency source: {fact.name}'
    if not tree.body:
        return f'# empty dependency source: {fact.name}'
    node = tree.body[0]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _function_stub(node)
    if isinstance(node, ast.ClassDef):
        return _class_stub(node)
    return fact.source


def _function_stub(node: ast.FunctionDef | ast.AsyncFunctionDef
                   ) -> str:
    lines = [f'@{ast.unparse(dec)}' for dec in node.decorator_list]
    args = ast.unparse(node.args)
    returns = (f' -> {ast.unparse(node.returns)}'
               if node.returns is not None else '')
    lines.append(f'def {node.name}({args}){returns}:')
    doc = ast.get_docstring(node)
    if doc:
        lines.append(_indent(f'"""{doc}"""'))
    lines.append(_indent('...'))
    return '\n'.join(lines)


def _class_stub(node: ast.ClassDef) -> str:
    lines = [f'@{ast.unparse(dec)}' for dec in node.decorator_list]
    bases = [ast.unparse(base) for base in node.bases]
    for keyword in node.keywords:
        value = ast.unparse(keyword.value)
        bases.append(f'{keyword.arg}={value}' if keyword.arg else value)
    signature = f'class {node.name}({", ".join(bases)})' \
        if bases else f'class {node.name}'
    lines.append(f'{signature}:')
    doc = ast.get_docstring(node)
    if doc:
        lines.append(_indent(f'"""{doc}"""'))
    for statement in node.body:
        if isinstance(statement, ast.Expr) and isinstance(
                statement.value, ast.Constant) and \
                isinstance(statement.value.value, str):
            continue                    # docstring already emitted
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # method SIGNATURE only -- bodies are not context
            method_lines = [f'@{ast.unparse(dec)}'
                            for dec in statement.decorator_list]
            args = ast.unparse(statement.args)
            returns = (f' -> {ast.unparse(statement.returns)}'
                       if statement.returns is not None else '')
            head = f'def {statement.name}({args}){returns}:'
            doc = ast.get_docstring(statement)
            body = [_indent('...')] if not doc else [
                _indent(f'"""{doc}"""'), _indent('...')]
            rendered = '\n'.join(
                [_indent(line) for line in method_lines]
                + [_indent(head), *body])
            lines.append(rendered)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign,
                                    ast.AugAssign, ast.Pass)):
            # class-level constants / TypeAlias / Protocol structure:
            # kept IN FULL -- targets may depend on them
            lines.append(_indent(ast.unparse(statement)))
        else:
            lines.append(_indent(ast.unparse(statement)))
    return '\n'.join(lines)


def _indent(text: str, prefix: str = '    ') -> str:
    return '\n'.join(
        prefix + line if line else line
        for line in text.split('\n'))
