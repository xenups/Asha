#!/usr/bin/env python3
"""Asha Phase 2 -- immutable versioned GraphState + online reconciliation.

Two layers, one state object (task 2.2/2.3):

    GraphState = normalized dependency-fact graph over the repository
                 (nodes/edges/reverse_edges) + a generation counter.
    DispatchIntent = a dispatch decision stamped with the GraphState
                 generation it was made against (task 2.5).

Immutability + atomic swap: GraphState coerces every field to a read-only
view (MappingProxyType over frozensets) in __post_init__; reconciliation
builds a COMPLETE candidate and replaces the state reference in one
assignment. Readers never observe a half-updated graph; on ANY failure
(candidate build, UNCERTAIN facts, cycle) the outcome carries the PREVIOUS
state object by reference and generation stays put (fail-closed).

Git purity (task 1.2): reconcile() never touches git -- callers feed it
(path, content) pairs they read read-only (orchestrator: `git show
<tree>:<path>` of verified worker result trees). No commits, no refs, no
working-tree mutation; the whole subsystem is in-memory.

Coalescing (task 2.4): the scheduler calls reconcile() ONCE per drained
completion batch with the union of observed files; one successful call =
exactly generation + 1, regardless of how many workers contributed.

Provider boundary: fact semantics come from dep_index (stdlib ast only,
no tree-sitter/NetworkX/Grimp). GraphBuilder traversal policy = which
fact relations cross file boundaries for node resolution (`_RESOLVE`,
extensible as a tuple -- relation semantics are NOT hard-coded in core,
they arrive from the provider vocabulary).
"""
from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from types import MappingProxyType

from . import dep_index

# Relations whose targets may name another local file (resolution policy
# supplied at the provider->builder boundary; provider owns semantics).
_RESOLVE_RELATIONS = frozenset({"imports", "depends_on"})


def _readonly_state(generation: int, nodes: Mapping[str, str],
                    edges: Mapping[str, Iterable[str]],
                    reverse_edges: Mapping[str, Iterable[str]] | None,
                    ) -> GraphState:
    """Coerce a hand-built mapping set into an immutable GraphState."""
    frozen_edges = MappingProxyType(
        {src: frozenset(targets) for src, targets in edges.items()})
    if reverse_edges is None:
        derived: dict[str, set[str]] = {}
        for src, targets in frozen_edges.items():
            for target in targets:
                derived.setdefault(target, set()).add(src)
        reverse_edges = {key: frozenset(value)
                         for key, value in derived.items()}
    frozen_reverse = MappingProxyType(
        {key: frozenset(value) for key, value in reverse_edges.items()})
    frozen_nodes = MappingProxyType(dict(nodes))
    state = object.__new__(GraphState)
    object.__setattr__(state, "generation", generation)
    object.__setattr__(state, "nodes", frozen_nodes)
    object.__setattr__(state, "edges", frozen_edges)
    object.__setattr__(state, "reverse_edges", frozen_reverse)
    return state


@dataclass(frozen=True)
class GraphState:
    """Immutable graph snapshot; version = generation (task 2.2)."""

    generation: int
    nodes: Mapping[str, str]
    edges: Mapping[str, frozenset[str]]
    reverse_edges: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        # True immutability: any attempted mutation raises TypeError.
        object.__setattr__(
            self, "nodes", MappingProxyType(dict(self.nodes)))
        object.__setattr__(
            self, "edges",
            MappingProxyType({src: frozenset(targets)
                              for src, targets in self.edges.items()}))
        object.__setattr__(
            self, "reverse_edges",
            MappingProxyType({key: frozenset(value)
                              for key, value
                              in self.reverse_edges.items()}))

    @classmethod
    def empty(cls) -> GraphState:
        """Generation-0 starting point (no facts analyzed yet)."""
        return cls(generation=0, nodes={}, edges={}, reverse_edges={})


@dataclass(frozen=True)
class DispatchIntent:
    """A dispatch decision stamped with the graph it was made against."""

    node_id: str
    generation: int

    def matches(self, state: GraphState) -> bool:
        """§2.5: equal generations -> PROCEED; mismatch -> DISCARD."""
        return self.generation == state.generation


@dataclass(frozen=True)
class ReconcileOutcome:
    """Result of one reconciliation attempt (success or fail-closed)."""

    ok: bool
    state: GraphState        # candidate on success, PREVIOUS ref on failure
    generation: int          # == state.generation
    reason: str | None       # 'uncertain:...' / 'cycle:...' on failure
    files: tuple[str, ...]   # the batch's updated paths
    affected: tuple[str, ...]
    added: int
    removed: int


def build_state(generation: int,
                edges: Mapping[str, Iterable[str]]) -> GraphState:
    """Build a state with reverse_edges + nodes derived (never manual)."""
    resolved_edges = {}
    for src, targets in edges.items():
        resolved_edges[src] = frozenset(
            target for target in targets)
    return _readonly_state(generation, _nodes_of(resolved_edges),
                           resolved_edges, None)


def _nodes_of(edges: Mapping[str, frozenset[str]]) -> dict[str, str]:
    """nodes[id] = 'file' for local paths, 'external' for raw names."""
    endpoint: set[str] = set(edges)
    for targets in edges.values():
        endpoint.update(targets)
    return {node: ("file" if node.endswith(".py") else "external")
            for node in endpoint}


def _resolve(source_path: str, target: str,
             known_files: frozenset[str]) -> str:
    """Map a provider target onto a local file when one is KNOWN.

    Unknown targets stay as the raw provider string (a leaf node) --
    never invented, never dropped (UNKNOWN != EMPTY at the builder too).
    """
    if target.startswith("."):
        level = len(target) - len(target.lstrip("."))
        stem = target.lstrip(".")
        base = posixpath.dirname(source_path)
        for _ in range(max(0, level - 1)):
            base = posixpath.dirname(base)
        prefix = f"{base}/" if base else ""
        stem_path = stem.replace(".", "/")
        candidates = (f"{prefix}{stem_path}.py" if stem_path else "",
                      f"{prefix}{stem_path}/__init__.py" if stem_path
                      else f"{prefix}__init__.py")
    else:
        stem_path = target.replace(".", "/")
        candidates = (f"{stem_path}.py", f"{stem_path}/__init__.py")
    for candidate in candidates:
        if candidate and candidate in known_files:
            return candidate
    return target


def reconcile(state: GraphState,
              index: dep_index.DependencyIndex,
              updates: Mapping[str, str | None],
              ) -> ReconcileOutcome:
    """One online reconciliation pass (task 2.3), git-free by contract.

    steps: reparse only updated files (index cache) -> UNCERTAIN gate ->
    affected set via reverse_edges -> delta edges -> candidate ->
    TopologicalSorter cycle gate -> atomic swap (generation + 1).
    Any failure returns the previous `state` BY REFERENCE (retained, not
    rebuilt) with generation unchanged.
    """
    files = tuple(updates.keys())
    # Section 12 (add): raw targets live outside the known set; capture
    # it BEFORE this batch so promotion is detectable afterwards.
    known_before = frozenset(index.known_paths())
    analyzed: dict[str, tuple[dep_index.DependencyFact, ...] | None] = {}
    uncertain: list[str] = []
    for path, content in updates.items():
        if content is None:          # path absent from that tree: removal
            analyzed[path] = None
            continue
        facts = index.analyze(path, content)
        if any(fact.confidence == dep_index.UNCERTAIN for fact in facts):
            uncertain.append(path)
        analyzed[path] = facts
    if uncertain:
        # Fail-closed: UNKNOWN never becomes EMPTY and never publishes.
        return ReconcileOutcome(
            ok=False, state=state, generation=state.generation,
            reason="uncertain:" + ",".join(uncertain), files=files,
            affected=(), added=0, removed=0)

    # Affected region: reverse closure over the CURRENT state (2.3.3).
    affected_list: list[str] = []
    seen: set[str] = set()
    queue = list(files)
    while queue:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.add(node)
        affected_list.append(node)
        queue.extend(state.reverse_edges.get(node, ()))

    # Candidate edge set: everything already known + delta for the batch.
    known_files = frozenset(index.known_paths())
    out: dict[str, set[str]] = {
        src: set(targets) for src, targets in state.edges.items()}
    added = 0
    removed = 0
    for path, path_facts in analyzed.items():
        old_targets = out.get(path, set())
        if path_facts is None:
            new_targets: set[str] = set()
        else:
            new_targets = {
                _resolve(path, fact.target, known_files)
                for fact in path_facts
                if fact.relation in _RESOLVE_RELATIONS}
            # call-facts keep raw symbol nodes: structural graph only
            # traverses import/depends_on relations (provider policy).
        added += len(new_targets - old_targets)
        removed += len(old_targets - new_targets)
        if new_targets:
            out[path] = new_targets
        else:
            out.pop(path, None)

    # Section 12 (add / rename-as-delete+add): a previously unresolved
    # RAW target is promoted to a real local edge once the provider file
    # enters the known set -- only resolution changes, no source is
    # reparsed (DependencyIndex cache stays authoritative, section 20).
    deleted = {path for path, facts in analyzed.items() if facts is None}
    known_now = frozenset(index.known_paths()) - deleted
    for src, targets in out.items():
        if src in analyzed:
            continue  # re-analyzed sources were fully resolved above
        for target in list(targets):
            if target in known_before or target in known_now:
                continue  # resolved local path already: never re-mangled
            promoted = _resolve(src, target, known_now)
            if promoted != target:
                targets.discard(target)
                targets.add(promoted)
                added += 1
                removed += 1
                if src not in affected_list:
                    affected_list.append(src)

    # Cycle gate (2.3.5): fail-closed, previous state retained.
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for src, targets in out.items():
        if targets:
            sorter.add(src, *targets)
    try:
        sorter.prepare()
    except CycleError as exc:
        cycle = (list(exc.args[1]) if len(exc.args) > 1
                 and isinstance(exc.args[1], list) else [])
        return ReconcileOutcome(
            ok=False, state=state, generation=state.generation,
            reason="cycle:" + ",".join(dict.fromkeys(cycle)),
            files=files, affected=tuple(affected_list),
            added=added, removed=removed)

    frozen_edges = MappingProxyType(
        {src: frozenset(targets) for src, targets in out.items()})
    candidate = _readonly_state(
        state.generation + 1, _nodes_of(frozen_edges), frozen_edges, None)
    return ReconcileOutcome(
        ok=True, state=candidate, generation=candidate.generation,
        reason=None, files=files, affected=tuple(affected_list),
        added=added, removed=removed)


__all__ = [
    "DispatchIntent", "GraphState", "ReconcileOutcome",
    "build_state", "reconcile",
]

_ = field  # keep dataclasses.field import honest for future payload states
