"""Phase 4.1 -- CodeGraph: explicit dependency edges, stable identity.

Nodes are STRING ids -- never ``id()`` objects, never memory identity:

``sym:<module>:<qualified>``   a symbol provided as an index
``mod:<module>``               a provided module as a whole
``ext:<dotted>``               external/stdlib import boundary
``builtin:<name>``             builtin (external boundary)
``unk:<token>``                UNRESOLVED dependency (kept, never dropped)

Edges carry the semantic kind that produced them (reference, call,
import, base, type, decorator, attribute). The graph represents cycles
plainly (``A -> B -> A`` is legal); ``strongly_connected`` computes SCCs
(Tarjan) and ``component_dag`` condenses them, so consumers that need a
DAG can have one WITHOUT deleting a single real dependency edge.

Closure expands local/project nodes and stops exactly at the
EXTERNAL / UNRESOLVED boundaries, which are *reported*, not traversed.
Determinism: node/edge/scc lists are sorted; output never depends on
insertion order or hash randomization.
"""
from __future__ import annotations

from dataclasses import dataclass

from .ast_indexer import ModuleIndex, SymbolFacts, is_builtin

BOUNDARY_LOCAL = 'LOCAL'
BOUNDARY_PROJECT = 'PROJECT'
BOUNDARY_EXTERNAL = 'EXTERNAL'
BOUNDARY_UNRESOLVED = 'UNRESOLVED'

_EDGE_KINDS = frozenset({
    'reference', 'call', 'import', 'base', 'type', 'decorator',
    'attribute',
})


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: str


@dataclass(frozen=True)
class CodeGraph:
    nodes: tuple[str, ...]
    edges: tuple[GraphEdge, ...]
    # node -> 'LOCAL'|'PROJECT'|'EXTERNAL'|'UNRESOLVED' (its identity,
    # computed at build time from the provided module set)
    boundaries: tuple[tuple[str, str], ...]

    def boundary_of(self, node: str) -> str:
        for candidate, boundary in self.boundaries:
            if candidate == node:
                return boundary
        return BOUNDARY_UNRESOLVED

    def edges_from(self, node: str) -> tuple[GraphEdge, ...]:
        return tuple(edge for edge in self.edges if edge.source == node)


@dataclass(frozen=True)
class ClosureResult:
    roots: tuple[str, ...]
    reachable: tuple[str, ...]        # sorted, roots included
    local: tuple[str, ...]            # same-module nodes
    project: tuple[str, ...]          # other provided modules
    external: tuple[str, ...]         # external boundary (not expanded)
    unresolved: tuple[str, ...]       # unresolved boundary (kept)
    expanded: int


@dataclass(frozen=True)
class SccResult:
    components: tuple[tuple[str, ...], ...]   # sorted, SCC members sorted
    component_edges: tuple[tuple[int, int], ...]  # DAG of components


def sym_node(module: str, name: str) -> str:
    return f'sym:{module}:{name}'


def build_graph(indices: tuple[ModuleIndex, ...]) -> CodeGraph:
    """Pure, in-memory: only the provided indices -- no filesystem,
    no import machinery, no clock, no environment."""
    provided = {index.module for index in indices}
    nodes: set[str] = set()
    edges: set[GraphEdge] = set()
    boundaries: dict[str, str] = {}

    def add_node(node: str, boundary: str) -> None:
        nodes.add(node)
        # first writer wins except UNRESOLVED never downgrades a
        # resolved identity
        current = boundaries.get(node)
        if current is None or (
                current == BOUNDARY_UNRESOLVED
                and boundary != BOUNDARY_UNRESOLVED):
            boundaries[node] = boundary

    def resolve(name: str, index: ModuleIndex) -> tuple[str, str]:
        """(node, boundary) for a bare name reference.

        Phase 5.1 cascade: local binding is settled by the caller
        (resolve_name), then module namespace -> module symbol ->
        imported symbol -> builtin -> UNRESOLVED.
        """
        # Repair B: interpreter-owned module namespace symbols resolve
        # to the MODULE node itself, context-sensitively. __path__
        # exists only on packages -- a non-package module must NOT
        # resolve it (hard lock: it stays UNRESOLVED below).
        if name in _MODULE_DUNDERS or (
                name == '__path__'
                and _is_package(index.module, provided)):
            return f'mod:{index.module}', BOUNDARY_LOCAL
        if index.symbol(name) is not None:
            node = sym_node(index.module, name)
            return node, BOUNDARY_LOCAL
        alias = index.alias_table().get(name)
        if alias is not None:
            # Repair C: relative bindings travel with their level and
            # canonicalise through the SAME import-target path as
            # import edges (level-0 behaviour byte-identical).
            module_name, original, level = alias
            return resolve_import_target(module_name, original, level,
                                         index.module)
        if is_builtin(name):
            return f'builtin:{name}', BOUNDARY_EXTERNAL
        return f'unk:{name}', BOUNDARY_UNRESOLVED

    def resolve_import_target(module: str, original: str | None,
                              level: int, consumer: str
                              ) -> tuple[str, str]:
        if level > 0:
            resolved_module = _relative(consumer, module, level,
                                        provided)
        else:
            resolved_module = module
        if not resolved_module:
            return f'unk:{module or "."}', BOUNDARY_UNRESOLVED
        if resolved_module in provided:
            if original:
                target_index = _find(indices, resolved_module)
                if target_index is not None and \
                        target_index.symbol(original) is not None:
                    return (sym_node(resolved_module, original),
                            BOUNDARY_PROJECT)
                return f'mod:{resolved_module}', BOUNDARY_PROJECT
            return f'mod:{resolved_module}', BOUNDARY_PROJECT
        dotted = (f'{resolved_module}.{original}'
                  if original else resolved_module)
        return f'ext:{dotted}', BOUNDARY_EXTERNAL

    for index in indices:
        module_node = f'mod:{index.module}'
        add_node(module_node, BOUNDARY_PROJECT)
        for fact in index.symbols:
            add_node(sym_node(index.module, fact.name), BOUNDARY_LOCAL)
        for fact in index.symbols:
            source = sym_node(index.module, fact.name)
            _symbol_edges(
                fact, source, index, resolve, edges, add_node)

        for import_fact in index.imports:
            if import_fact.kind == 'star':
                target = f'unk:star:{import_fact.module or "."}'
                add_node(target, BOUNDARY_UNRESOLVED)
                edges.add(GraphEdge(module_node, target, 'import'))
                continue
            if import_fact.kind == 'import':
                for original, _alias in import_fact.names:
                    target, boundary = resolve_import_target(
                        original, None, 0, index.module)
                    add_node(target, boundary)
                    edges.add(GraphEdge(module_node, target, 'import'))
            else:
                for original, _alias in import_fact.names:
                    target, boundary = resolve_import_target(
                        import_fact.module, original,
                        import_fact.level, index.module)
                    add_node(target, boundary)
                    edges.add(GraphEdge(module_node, target, 'import'))

        for marker in index.unresolved:
            target = f'unk:{marker}'
            add_node(target, BOUNDARY_UNRESOLVED)
            edges.add(GraphEdge(module_node, target, 'reference'))

    return CodeGraph(
        nodes=tuple(sorted(nodes)),
        edges=tuple(sorted(
            edges, key=lambda edge: (edge.source, edge.target,
                                     edge.kind))),
        boundaries=tuple(sorted(boundaries.items())),
    )


def _symbol_edges(
        fact: SymbolFacts, source: str, index: ModuleIndex,
        resolve: object, edges: set[GraphEdge], add_node: object) -> None:
    """Edges out of one symbol: reference/call/type/base/decorator.

    Dotted names resolve through their ROOT; when the root is a local
    (param/local binding) the reference stays INSIDE the symbol as a
    self-edge instead of becoming a bogus UNRESOLVED dependency.
    """

    def resolve_name(name: str) -> tuple[str, str]:
        resolver = resolve
        assert callable(resolver)
        if '.' in name:
            root = name.partition('.')[0]
            if root in fact.local_names:
                return source, BOUNDARY_LOCAL
            return resolver(root, index)  # type: ignore[operator]
        # Repair A: strict lexical cascade -- a BARE name with a
        # provable local binding stays inside this symbol (local
        # self-edge). No blanket fallback: unbound names fall
        # through to resolve() and become unk:* (negative lock).
        # The reads guard preserves ORDER: a name also loaded before
        # its first binding is a forward reference -- a real
        # NameError at runtime -- and must stay UNRESOLVED.
        if name in fact.local_names and name not in fact.reads:
            return source, BOUNDARY_LOCAL
        if name in fact.local_names and name in fact.reads:
            # Binding exists but the read sits in a forward position in
            # the fact's own set (walk-order artifact): if a DESCENDANT
            # fact of this qualified name also reads it, the read was
            # merged from a nested definition -- PEP 227 makes the
            # binding exist by call time, so it stays LOCAL. Without a
            # reading descendant there is no such definition and the
            # position stays a genuine forward reference, which falls
            # through to resolve() as UNRESOLVED.
            for other in index.symbols:
                if (other.name.startswith(fact.name + '.')
                        and name in other.reads):
                    return source, BOUNDARY_LOCAL
        # Closure: a name bound by a nested def/class in an ENCLOSING
        # symbol of this fact (fact names are qualified paths, so walk
        # the ancestors). Walk order may have recorded the read before
        # the ancestor's later def -- at call time the closure is
        # bound, so edge the reference to the ANCESTOR symbol.
        ancestor = fact.name
        while '.' in ancestor:
            ancestor = ancestor.rpartition('.')[0]
            parent = index.symbol(ancestor)
            if parent is not None and name in parent.local_names:
                return f'sym:{index.module}:{ancestor}', BOUNDARY_LOCAL
        return resolver(name, index)  # type: ignore[operator]

    def link(target: str, boundary: str, kind: str) -> None:
        edges.add(GraphEdge(source, target, kind))
        add_node(target, boundary)  # type: ignore[operator]

    for name in sorted(fact.reads):
        target, boundary = resolve_name(name)
        link(target, boundary, 'reference')
    for name in sorted(fact.calls):
        target, boundary = resolve_name(name)
        link(target, boundary, 'call')
    for name in sorted(fact.annotations):
        target, boundary = resolve_name(name)
        link(target, boundary, 'type')
    for name in sorted(fact.bases):
        target, boundary = resolve_name(name)
        link(target, boundary, 'base')
    for name in sorted(fact.decorators):
        target, boundary = resolve_name(name)
        link(target, boundary, 'decorator')
    for name in sorted(fact.mutations):
        target, boundary = resolve_name(name)
        link(target, boundary, 'reference')


def _find(indices: tuple[ModuleIndex, ...],
          module: str) -> ModuleIndex | None:
    for index in indices:
        if index.module == module:
            return index
    return None


_MODULE_DUNDERS = frozenset({
    '__name__', '__file__', '__doc__', '__package__', '__loader__',
    '__spec__', '__annotations__', '__builtins__',
})


def _is_package(module: str, provided: set[str]) -> bool:
    """Package-ness is knowable from the provided index: a package is
    a module whose children (submodules) were indexed alongside it.
    Conservative direction: a childless __init__ is treated as a plain
    module, so __path__ can only be UNDER-attributed, never wrong."""
    return any(other.startswith(module + '.')
               for other in provided)


def _relative(consumer_module: str, target: str, level: int,
              provided: set[str]) -> str:
    """Python relative-import semantics (Phase 5.1 repair C).

    Each dot anchors at the PACKAGE containing the import: the package
    itself when the consumer IS a package (its __init__), otherwise the
    consumer's parent package. The old parent-only formula returned ''
    for every relative import written inside a top-level package
    __init__, minting UNRESOLVED nodes. Excess depth (beyond the top
    package) resolves to '' so the caller poisons the edge fail-closed
    instead of guessing.
    """
    parts = consumer_module.split('.')
    anchor = parts if _is_package(consumer_module, provided) \
        else parts[:-1]
    up = level - 1
    if up > len(anchor):
        return ''
    base = anchor[:len(anchor) - up]
    if not base:
        return ''
    if target:
        return '.'.join([*base, target])
    return '.'.join(base)


def closure(graph: CodeGraph, roots: tuple[str, ...],
            target_module: str) -> ClosureResult:
    """BFS with a visited set: cycles terminate, boundaries report."""
    reachable: set[str] = set()
    unresolved: set[str] = set()
    external: set[str] = set()
    local: set[str] = set()
    project: set[str] = set()
    queue = [*roots]
    expanded = 0
    while queue:
        node = queue.pop(0)
        if node in reachable:
            continue
        reachable.add(node)
        boundary = graph.boundary_of(node)
        if boundary == BOUNDARY_UNRESOLVED:
            unresolved.add(node)
            continue
        if boundary == BOUNDARY_EXTERNAL:
            external.add(node)
            continue
        if node.startswith(f'sym:{target_module}:'):
            local.add(node)
        else:
            project.add(node)
        expanded += 1
        for edge in graph.edges_from(node):
            if edge.target not in reachable:
                queue.append(edge.target)
    return ClosureResult(
        roots=tuple(roots),
        reachable=tuple(sorted(reachable)),
        local=tuple(sorted(local)),
        project=tuple(sorted(project)),
        external=tuple(sorted(external)),
        unresolved=tuple(sorted(unresolved)),
        expanded=expanded,
    )


def strongly_connected(graph: CodeGraph) -> SccResult:
    """Tarjan SCC (iterative -- no recursion-limit ceilings), stable
    ordering: nodes visited in sorted order, components sorted."""
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[tuple[str, ...]] = []
    counter = 0
    node_set = set(graph.nodes)
    adjacency: dict[str, list[str]] = {
        node: sorted({edge.target for edge in graph.edges_from(node)
                      if edge.target in node_set})
        for node in graph.nodes
    }
    for root in graph.nodes:
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_index = work[-1]
            if child_index == 0:
                index_of[node] = counter
                lowlink[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            children = adjacency[node]
            if child_index < len(children):
                child = children[child_index]
                work[-1] = (node, child_index + 1)
                if child not in index_of:
                    work.append((child, 0))
                elif child in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(tuple(sorted(component)))
    components.sort()
    # condensation: DAG over component ids
    component_of: dict[str, int] = {}
    for position, group in enumerate(components):
        for member in group:
            component_of[member] = position
    component_edges: set[tuple[int, int]] = set()
    for edge in graph.edges:
        if edge.source not in component_of or edge.target not in component_of:
            continue
        source_component = component_of[edge.source]
        target_component = component_of[edge.target]
        if source_component != target_component:
            component_edges.add((source_component, target_component))
    return SccResult(
        components=tuple(components),
        component_edges=tuple(sorted(component_edges)),
    )


def component_dag(graph: CodeGraph) -> SccResult:
    """SCC components + condensation edges (a true DAG by
    construction). Real dependency edges are NEVER removed."""
    return strongly_connected(graph)
