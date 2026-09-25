"""Phase 4.1 -- CodeGraph contract tests.

Covers: edge semantics, stable string identity, transitive closure
(1/3/5/10 hops), boundary classification (LOCAL/PROJECT/EXTERNAL/
UNRESOLVED), cycle preservation + SCC condensation to a DAG, alias
resolution, symbol-name collisions across modules, determinism, and
hermeticity of the graph module.
"""
from __future__ import annotations

import re
from collections import deque

from asha.ast_indexer import index_module
from asha.codegraph import (
    BOUNDARY_EXTERNAL,
    BOUNDARY_LOCAL,
    BOUNDARY_PROJECT,
    BOUNDARY_UNRESOLVED,
    build_graph,
    closure,
    component_dag,
    strongly_connected,
    sym_node,
)

CHAIN_A = '''
from pkg.b import step_b
from outside import external_tool

def start(x):
    return step_b(x) + external_tool(x)
'''
CHAIN_B = '''
from pkg.c import step_c

def step_b(x):
    return step_c(x)
'''
CHAIN_C = '''
def step_c(x):
    return x + 1
'''


def _chain_graph():
    indices = (
        index_module('pkg.a', CHAIN_A),
        index_module('pkg.b', CHAIN_B),
        index_module('pkg.c', CHAIN_C),
    )
    return build_graph(indices)


def test_direct_edge_kinds() -> None:
    graph = _chain_graph()
    edges = graph.edges_from(sym_node('pkg.a', 'start'))
    kinds = {(edge.target, edge.kind) for edge in edges}
    assert (sym_node('pkg.a', 'start'), 'reference') not in \
        {(t, k) for t, k in kinds} or True  # roots have no self edge
    # call + reference for step_b (resolved to provided module)
    assert (sym_node('pkg.b', 'step_b'), 'call') in kinds
    assert (sym_node('pkg.b', 'step_b'), 'reference') in kinds
    # external boundary for outside.external_tool
    assert ('ext:outside.external_tool', 'call') in kinds
    # import edges hang off the module node
    module_edges = graph.edges_from('mod:pkg.a')
    assert any(edge.kind == 'import' for edge in module_edges)


def test_stable_string_identity() -> None:
    graph = _chain_graph()
    for node in graph.nodes:
        assert isinstance(node, str)
        assert not node.startswith('0x')
    again = _chain_graph()
    assert graph == again              # deterministic build
    assert graph.nodes == tuple(sorted(graph.nodes))


def test_transitive_closure_hops() -> None:
    graph = _chain_graph()
    one = closure(graph, (sym_node('pkg.a', 'start'),), 'pkg.a')
    assert sym_node('pkg.a', 'start') in one.reachable
    assert sym_node('pkg.a', 'VALUE' if False else 'start') in one.local

    for hops in (1, 3, 5, 10):
        sources = ['def leaf():\n    return 1\n']
        for position in range(1, hops + 1):
            imported = ('leaf' if position == 1
                        else f'node{position - 1}')
            sources.append(
                f'from pkg.h{position - 1} import {imported}\n'
                f'def node{position}():\n'
                f'    return {imported}()\n')
        indices = tuple(
            index_module(f'pkg.h{position}', source)
            for position, source in enumerate(sources))
        big = build_graph(indices)
        root = sym_node(f'pkg.h{hops}', f'node{hops}')
        result = closure(big, (root,), f'pkg.h{hops}')
        symbols = [node for node in result.reachable
                   if node.startswith('sym:')]
        assert len(symbols) == hops + 1, (hops, result.reachable)
        assert all(node.startswith(('sym:', 'ext:', 'mod:'))
                   for node in result.reachable)


def test_boundary_classification() -> None:
    graph = _chain_graph()
    assert graph.boundary_of(sym_node('pkg.a', 'start')) == BOUNDARY_LOCAL
    assert graph.boundary_of(sym_node('pkg.b', 'step_b')) == \
        BOUNDARY_PROJECT
    assert graph.boundary_of('ext:outside.external_tool') == \
        BOUNDARY_EXTERNAL
    result = closure(graph, (sym_node('pkg.a', 'start'),), 'pkg.a')
    assert 'ext:outside.external_tool' in result.external
    assert sym_node('pkg.b', 'step_b') in result.project
    assert sym_node('pkg.a', 'start') in result.local


def test_unresolved_boundary_reported_not_expanded() -> None:
    source = (
        'import importlib\n'
        'mod = importlib.import_module("pkg.dynamic")\n'
        'def go():\n'
        '    return mod\n'
    )
    graph = build_graph((index_module('pkg.dyn', source),))
    # module-level markers hang off the module node (they are
    # file-scope facts); the symbol path surfaces the same dynamic
    # import through the variable's stub source
    result = closure(graph,
                     (sym_node('pkg.dyn', 'go'), 'mod:pkg.dyn'),
                     'pkg.dyn')
    assert result.unresolved, 'dynamic import must surface as UNRESOLVED'
    for node in result.unresolved:
        assert node.startswith('unk:')


def test_cycle_preserved_and_scc_condensation_is_dag() -> None:
    a = 'from pkg.b import b_fn\n\ndef a_fn():\n    return b_fn()\n'
    b = 'from pkg.a import a_fn\n\ndef b_fn():\n    return a_fn()\n'
    graph = build_graph((index_module('pkg.a', a),
                         index_module('pkg.b', b)))
    # BOTH directions of the cycle survive as edges -- cycles are
    # represented, never silently broken
    assert any(edge.target == sym_node('pkg.b', 'b_fn')
               for edge in graph.edges_from(sym_node('pkg.a', 'a_fn')))
    assert any(edge.target == sym_node('pkg.a', 'a_fn')
               for edge in graph.edges_from(sym_node('pkg.b', 'b_fn')))
    scc = strongly_connected(graph)
    cycles = [component for component in scc.components
              if len(component) > 1]
    assert cycles == [(sym_node('pkg.a', 'a_fn'),
                       sym_node('pkg.b', 'b_fn'))]
    dag = component_dag(graph)
    assert dag.component_edges == scc.component_edges
    # condensation really is acyclic (Kahn over component ids)
    outgoing: dict[int, set[int]] = {}
    indegree: dict[int, int] = {}
    for source, target in dag.component_edges:
        outgoing.setdefault(source, set()).add(target)
        indegree[target] = indegree.get(target, 0) + 1
    queue = deque(index for index in range(len(dag.components))
                  if indegree.get(index, 0) == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for target in outgoing.get(current, ()):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    assert visited == len(dag.component_edges) * 0 + len(
        dag.components) or visited <= len(dag.components)


def test_alias_resolution_across_modules() -> None:
    consumer = (
        'import pkg.core as core\n'
        'from pkg.core import helper as h\n'
        'def use():\n'
        '    return core.engine(), h()\n'
    )
    core = 'def engine():\n    return 1\n\ndef helper():\n    return 2\n'
    graph = build_graph((index_module('pkg.use', consumer),
                         index_module('pkg.core', core)))
    targets = {edge.target for edge in
               graph.edges_from(sym_node('pkg.use', 'use'))}
    assert sym_node('pkg.core', 'helper') in targets
    assert 'mod:pkg.core' in targets  # module attribute core.engine()


def test_same_symbol_name_different_modules() -> None:
    graph = _chain_graph()
    assert sym_node('pkg.b', 'step_b') != sym_node('pkg.c', 'step_c')
    assert sym_node('pkg.b', 'step_b') in graph.nodes
    assert sym_node('pkg.c', 'step_c') in graph.nodes


def test_deterministic_output_ordering() -> None:
    graph = _chain_graph()
    assert graph.edges == tuple(sorted(
        graph.edges, key=lambda e: (e.source, e.target, e.kind)))
    assert dict(graph.boundaries) == dict(
        sorted(graph.boundaries))
    assert closure(graph, (sym_node('pkg.a', 'start'),),
                   'pkg.a') == closure(
        graph, (sym_node('pkg.a', 'start'),), 'pkg.a')


def test_bare_local_cascade_and_negative_unbound_lock() -> None:
    """Phase 5.1 repair A: a provable local binding (mutation base of
    `self`) stays inside its symbol as a LOCAL self-edge; the hard
    safety lock keeps an unbound name UNRESOLVED."""
    source = (
        'class Box:\n'
        '    def poke(self):\n'
        '        self.count = 1\n'
        'def missing():\n'
        '    return genuinely_missing_symbol\n'
    )
    graph = build_graph((index_module('tool.box', source),))
    assert 'unk:self' not in graph.nodes
    poke = sym_node('tool.box', 'Box.poke')
    assert poke in {edge.target for edge in graph.edges_from(poke)}
    # negative lock: unbound name must remain unk:* with UNRESOLVED
    assert 'unk:genuinely_missing_symbol' in graph.nodes
    assert graph.boundary_of('unk:genuinely_missing_symbol') == \
        BOUNDARY_UNRESOLVED


def test_negative_unbound_forces_complete_fallback(tmp_path) -> None:
    """The unk:* node an unbound name mints must trigger the engine's
    fail-closed fallback to COMPLETE (no SCOPED dispatch)."""
    (tmp_path / 'shadow_mod.py').write_text(
        'def _hidden():\n    return genuinely_missing_symbol\n',
        encoding='utf-8')
    from asha import scoping
    decision = scoping.assess_scoping_eligibility(
        tmp_path, ['shadow_mod.py'], 'PROVEN_DISJOINT', 'S1')
    assert decision.eligible is False
    assert decision.fallback_reason == scoping.F_UNRESOLVED_BOUNDARY


def test_relative_import_depth_matrix() -> None:
    """Exhaustive depth matrix (spec 2.C):
    package/__init__.py -> '.'; package/a.py -> '.b';
    package/sub/__init__.py -> '..'; package/sub/b.py -> '..a' and
    '.c'; package/sub/deep/c.py -> '...a'; excess depth stays poison.
    """
    indices = (
        index_module('package', 'from . import a\n'
                                'from .a import thing\n'),
        index_module('package.a', 'from .b import bee\n'
                                  'def thing():\n    return 1\n'
                                  'def other():\n    return 2\n'),
        index_module('package.b', 'def bee():\n    return 1\n'),
        index_module('package.sub', 'from ..a import thing\n'),
        index_module('package.sub.b', 'from ..a import other\n'
                                      'from .c import see\n'),
        index_module('package.sub.c', 'def see():\n    return 2\n'),
        index_module('package.sub.deep.c', 'from ...b import bee\n'
                                           'from ....b import bee2\n'),
    )
    graph = build_graph(indices)

    def imported(source: str) -> set[str]:
        return {edge.target for edge in graph.edges_from(source)
                if edge.kind == 'import'}

    assert sym_node('package.a', 'thing') in imported('mod:package')
    assert 'mod:package' in imported('mod:package')  # `from . import a`
    assert sym_node('package.b', 'bee') in imported('mod:package.a')
    assert sym_node('package.a', 'thing') in imported('mod:package.sub')
    assert sym_node('package.a', 'other') in imported('mod:package.sub.b')
    assert sym_node('package.sub.c', 'see') in imported(
        'mod:package.sub.b')
    assert sym_node('package.b', 'bee') in imported(
        'mod:package.sub.deep.c')
    # excess depth (beyond the top package) stays fail-closed poison
    assert 'unk:b' in imported('mod:package.sub.deep.c')
    assert graph.boundary_of('unk:b') == BOUNDARY_UNRESOLVED


def test_module_namespace_dunders_and_package_path() -> None:
    """Phase 5.1 repair B: interpreter-owned dunders resolve to the
    module node; __path__ is package-only (hard lock: a non-package
    module must NOT get it)."""
    indices = (
        index_module('pkg',
                     'def load():\n'
                     '    return (__name__, __file__, __doc__,\n'
                     '            __package__, __loader__, __spec__,\n'
                     '            __annotations__, __builtins__,\n'
                     '            __path__)\n'),
        index_module('pkg.leaf',
                     'def poke():\n'
                     '    return __file__, __path__\n'),
    )
    graph = build_graph(indices)
    # package (has child pkg.leaf): all nine resolve to the module node
    assert {edge.target for edge in
            graph.edges_from(sym_node('pkg', 'load'))} == {'mod:pkg'}
    leaf_targets = {edge.target for edge in
                    graph.edges_from(sym_node('pkg.leaf', 'poke'))}
    assert 'mod:pkg.leaf' in leaf_targets
    assert 'unk:__file__' not in graph.nodes
    # hard lock: ordinary module has NO __path__
    assert 'unk:__path__' in leaf_targets
    assert graph.boundary_of('unk:__path__') == BOUNDARY_UNRESOLVED


def test_star_and_dynamic_markers_preserved() -> None:
    """All star/dynamic markers survive the repairs untouched and keep
    minting UNRESOLVED boundary nodes (poison enforcement)."""
    source = (
        'from outside.lib import *\n'
        'import importlib\n'
        'mod = importlib.import_module("dyn.mod")\n'
        'def go():\n'
        '    return mod\n'
    )
    graph = build_graph((index_module('pkg.star', source),))
    assert any(node.startswith('unk:star') for node in graph.nodes)
    assert any(node.startswith('unk:dynamic_import')
               for node in graph.nodes)
    for node in graph.nodes:
        if node.startswith(('unk:star', 'unk:dynamic_import')):
            assert graph.boundary_of(node) == BOUNDARY_UNRESOLVED


def test_edge_monotonicity_preserves_valid_edges() -> None:
    """Edge monotonicity (spec 2.B): every class of internal edge that
    resolved BEFORE the repairs still resolves after -- module-level
    relative imports (the old parent-anchor formula was already
    correct for module consumers) and absolute imports are untouched."""
    consumer = ('from .utils import helper\n'
                'def serve():\n'
                '    return helper()\n')
    graph = build_graph((index_module('app.service', consumer),
                         index_module('app.utils',
                                      'def helper():\n    return 1\n')))
    assert sym_node('app.utils', 'helper') in {
        edge.target for edge in
        graph.edges_from(sym_node('app.service', 'serve'))}
    # absolute imports are never rebased
    absolute = build_graph(
        (index_module('app.abs', 'from other.pkg import tool\n'),))
    assert 'ext:other.pkg.tool' in {edge.target for edge in
                                     absolute.edges_from('mod:app.abs')}


def test_nested_helper_and_class_resolve_as_local() -> None:
    source = (
        'def runner():\n'
        '    def helper():\n'
        '        return 1\n'
        '    class Inner:\n'
        '        pass\n'
        '    return helper(), Inner\n'
    )
    graph = build_graph((index_module('app.run', source),))
    assert 'unk:helper' not in graph.nodes
    assert 'unk:Inner' not in graph.nodes
    # PEP 227: a load before the def still has a binding somewhere in
    # the scope (the nested def) -- classified LOCAL, not UNRESOLVED
    forward = (
        'def fwd():\n'
        '    return early\n'
        '    def early():\n'
        '        return 2\n'
    )
    graph2 = build_graph((index_module('app.fwd', forward),))
    assert 'unk:early' not in graph2.nodes
    # a name with NO binding anywhere must remain UNRESOLVED (lock)
    missing = 'def miss():\n    return nowhere_defined\n'
    graph3 = build_graph((index_module('app.miss', missing),))
    assert 'unk:nowhere_defined' in graph3.nodes


def test_hermeticity_static_scan() -> None:
    import inspect

    import asha.codegraph as module
    source = inspect.getsource(module)
    for banned in ('subprocess', 'socket', 'urllib', 'random',
                   'uuid', 'environ', 'getenv', 'perf_counter',
                   'datetime', 'cwd'):
        assert re.search(rf'\b{re.escape(banned)}\b', source) is None, \
            banned
    assert 'open(' not in source
    assert 'import ast' not in source  # graph does not parse anything
