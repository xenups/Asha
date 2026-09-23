#!/usr/bin/env python3
"""Asha Phase 2 -- normalized dependency fact extraction (stdlib ast only).

One provider only: Python's built-in `ast`. NO tree-sitter, NO external
graph package (Phase 2 constraint 1.1). Relation semantics live HERE (the
provider boundary): the core carries facts as opaque (source, target,
relation, location, confidence) tuples and never interprets them beyond
traversal rules supplied by the graph builder.

Uncertainty model (Phase-1 law preserved):

    dynamic import  -> DependencyFact(confidence=UNCERTAIN, target=...)
    parse failure   -> DependencyFact(confidence=UNCERTAIN, target='*')

UNKNOWN is never silently dropped as "no dependency": UNCERTAIN facts are
emitted, and graph_state.reconcile() turns ANY of them into a fail-closed
reconciliation (previous GraphState retained, no generation bump).

Cache contract (2.1): keyed by ``(file_path, content_sha256)``; identical
(path, sha) hits the cache and never parses again (`parse_count` counts
actual ``ast.parse`` calls and is the test oracle). Cache entries are
pure function results -- parser/provider upgrades must be shipped as a
code change that invalidates this module's semantics (a process restart
drops the in-memory cache; persistence is deliberately not Phase 2).
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

CERTAIN = "CERTAIN"
UNCERTAIN = "UNCERTAIN"

SUPPORTED_RELATIONS = frozenset({"depends_on", "imports", "calls"})

# Call forms that make the import target indeterminable statically.
_DYNAMIC_CALLS = frozenset({"__import__", "import_module"})


@dataclass(frozen=True)
class DependencyFact:
    """Normalized fact (2.1). Immutable: facts are cache payloads."""

    source: str      # file path the fact was extracted from
    target: str      # module/symbol as written; '*' when unknown
    relation: str    # one of SUPPORTED_RELATIONS (provider semantics)
    location: str    # 'path:line' provenance (line 0 = whole file)
    confidence: str  # CERTAIN | UNCERTAIN


class DependencyIndex:
    """In-memory fact cache keyed by (file_path, content_sha256)."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[DependencyFact, ...]] = {}
        self.parse_count = 0  # actual ast.parse calls (test oracle)

    def known_paths(self) -> tuple[str, ...]:
        """Every path ever analyzed (uncertain entries included)."""
        return tuple(sorted({key[0] for key in self._cache}))

    @staticmethod
    def content_sha256(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def analyze(self, path: str, content: str) -> tuple[DependencyFact, ...]:
        """Facts for (path, content); parse at most once per key."""
        key = (path, self.content_sha256(content))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        facts = self._extract(path, content)
        self._cache[key] = facts
        return facts

    # -- extraction (the only place ast.parse is allowed) -------------------

    def _extract(self, path: str, content: str) -> tuple[DependencyFact, ...]:
        self.parse_count += 1
        try:
            tree = ast.parse(content, filename=path)
        except (SyntaxError, ValueError, TypeError):
            # Parse failure yields UNKNOWN (UNCERTAIN), never an empty set.
            return (DependencyFact(
                source=path, target="*", relation="depends_on",
                location=f"{path}:0", confidence=UNCERTAIN),)
        found: dict[tuple[str, str, str, str, str], DependencyFact] = {}

        def emit(target: str, relation: str, location: str,
                 confidence: str) -> None:
            fact = DependencyFact(source=path, target=target,
                                  relation=relation, location=location,
                                  confidence=confidence)
            found[(fact.source, fact.target, fact.relation,
                   fact.location, fact.confidence)] = fact

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                location = f"{path}:{node.lineno}"
                for alias in node.names:
                    emit(alias.name, "imports", location, CERTAIN)
            elif isinstance(node, ast.ImportFrom):
                location = f"{path}:{node.lineno}"
                if node.module is None:
                    # `from . import leaf` -> one fact per imported name.
                    prefix = "." * node.level
                    for alias in node.names:
                        if alias.name == "*":
                            emit(prefix or ".", "imports", location, CERTAIN)
                        else:
                            emit(prefix + alias.name, "imports",
                                 location, CERTAIN)
                else:
                    emit("." * node.level + node.module, "imports",
                         location, CERTAIN)
            elif isinstance(node, ast.Call):
                dotted = self._dotted(node.func)
                if dotted is not None and self._is_dynamic(dotted):
                    # Dynamic import: target when literal, '*' otherwise;
                    # always UNCERTAIN (fail-closed upstream).
                    arg = node.args[0] if node.args else None
                    target = (arg.value if isinstance(arg, ast.Constant)
                              and isinstance(arg.value, str) else "*")
                    emit(str(target), "imports", f"{path}:{node.lineno}",
                         UNCERTAIN)
                elif dotted is not None:
                    emit(dotted, "calls", f"{path}:{node.lineno}", CERTAIN)
        return tuple(found.values())

    @staticmethod
    def _dotted(node: ast.AST) -> str | None:
        """Best-effort dotted name; None for anything not a plain chain."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = DependencyIndex._dotted(node.value)
            return None if base is None else f"{base}.{node.attr}"
        return None

    @staticmethod
    def _is_dynamic(dotted: str) -> bool:
        return (dotted in _DYNAMIC_CALLS
                or dotted.endswith(".import_module"))
