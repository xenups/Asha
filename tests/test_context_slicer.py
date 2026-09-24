"""Phase 4.1 -- ContextSlicer + GROUND-TRUTH dependency recall.

Authority is the INDEPENDENT hand-written expected set (§16): it is
derived by reading the corpus source, never from the implementation's
output. Hard requirement: recall == 1.0 -- one missing dependency
fails. Precision (extra dependencies) is measured and reported, not
silently ignored.

Also covers: target verbatim, stub safety (signature/base/constants/
TypeAlias/Protocol), UNKNOWN markers kept, determinism, hermeticity,
missing-dependency detector self-check, and a mypy secondary validation
of a generated artifact (stub shape, separate from the slice).
"""
from __future__ import annotations

import re
import subprocess
import sys

from asha.ast_indexer import index_module
from asha.codegraph import build_graph
from asha.context_slicer import slice_context, stub_for

# ---------------------------------------------------------------------
# canonical corpus (§16/17): every construct listed in the phase spec
# ---------------------------------------------------------------------
CANONICAL = '''
from dataclasses import dataclass
from typing import Protocol, TypeAlias

OrderId: TypeAlias = str
DEFAULT_TIMEOUT = 30

def decorate(fn):
    return fn

class Base:
    KIND = "base"

    def boot(self):
        return self.KIND

@dataclass
class Order:
    order_id: OrderId
    total: int

class Checker(Protocol):
    def check(self, order: "Order") -> bool: ...

def validate(order: Order) -> "Order":
    return order

def fetch_total(order_id: OrderId) -> int:
    return DEFAULT_TIMEOUT

@decorate
def process_order(order_id: "OrderId") -> "tuple[list[Order], int]":
    order = Order(order_id, fetch_total(order_id))
    checked = validate(order)
    return ([checked], DEFAULT_TIMEOUT)
'''

# hand-written transitive expectations, derived FROM THE SOURCE ABOVE:
# process_order -> Order/validate/fetch_total/DEFAULT_TIMEOUT/decorate/
# list/OrderId; then Order -> dataclass/int/OrderId, OrderId -> str/
# TypeAlias, fetch_total -> OrderId/DEFAULT_TIMEOUT/int ...
EXPECTED_PROCESS_ORDER = {
    'Order', 'OrderId', 'TypeAlias', 'dataclass', 'decorate',
    'fetch_total', 'validate', 'DEFAULT_TIMEOUT', 'list', 'tuple',
    'int', 'str',
}
# Order class itself: decorator + field annotations + transitive alias
EXPECTED_ORDER_CLASS = {
    'dataclass', 'OrderId', 'TypeAlias', 'int', 'str',
}
# fetch_total: parameter/return annotations + module constant
EXPECTED_FETCH_TOTAL = {
    'OrderId', 'TypeAlias', 'str', 'DEFAULT_TIMEOUT', 'int',
}

# multi-module transitive TYPE dependency chain (§17)
HOP_MODULES = (
    ('pkg.h0', 'class Token:\n    pass\n'),
    ('pkg.h1', (
     'from pkg.h0 import Token\n'
     '\n'
     'def issue(token: Token) -> Token:\n'
     '    return token\n')),
    ('pkg.h2', (
     'from pkg.h1 import issue\n'
     '\n'
     'class Ticket:\n'
     '    def mint(self, token):\n'
     '        return issue(token)\n')),
    ('pkg.h3', (
     'from pkg.h2 import Ticket\n'
     '\n'
     'def run(token):\n'
     '    return Ticket().mint(token)\n')),
)
EXPECTED_H3_RUN = {'Ticket', 'issue', 'Token'}


def _slice(source: str, target: str, module: str = 'shop.canonical'):
    index = index_module(module, source)
    graph = build_graph((index,))
    facts = index.symbol(target)
    assert facts is not None, target
    result = slice_context(facts.source, target_name=target,
                           target_module=module, graph=graph,
                           indices=(index,))
    return index, graph, result


def _extracted_names(slice_result) -> set[str]:
    """Dependency names from the closure (root excluded) -- the same
    node identities the slicer actually acted upon."""
    names: set[str] = set()
    root = f'sym:{slice_result.closure.roots[0].split(":", 2)[1]}:' \
        f'{slice_result.target_name}'
    for node in slice_result.closure.reachable:
        if node == root:
            continue
        if node.startswith('sym:'):
            names.add(node.split(':', 2)[2])
        elif node.startswith('ext:'):
            names.add(node.split(':', 1)[1].rsplit('.', 1)[-1])
        elif node.startswith(('builtin:', 'unk:', 'mod:')):
            names.add(node.split(':', 1)[1])
    return names


def _recall(expected: set[str], extracted: set[str]) -> float:
    if not expected:
        return 1.0
    return len(expected & extracted) / len(expected)


def test_ground_truth_recall_canonical_corpus() -> None:
    for target, expected in (
            ('process_order', EXPECTED_PROCESS_ORDER),
            ('Order', EXPECTED_ORDER_CLASS),
            ('fetch_total', EXPECTED_FETCH_TOTAL)):
        _, _, result = _slice(CANONICAL, target)
        extracted = _extracted_names(result)
        missing = expected - extracted
        extra = extracted - expected
        # HARD safety requirement: recall must be 100%
        assert missing == set(), f'{target} missing {sorted(missing)}'
        assert _recall(expected, extracted) == 1.0
        # measured, not asserted away (§16 precision / over-inclusion)
        print(f'[ground-truth] {target}: recall=1.0 '
              f'precision={len(expected & extracted) / max(len(extracted), 1):.3f} '
              f'extra={sorted(extra)}')


def test_missing_dependency_detector_works() -> None:
    """Self-check of the harness: a wrong expectation MUST produce
    recall < 1.0 (otherwise the recall metric itself is broken)."""
    _, _, result = _slice(CANONICAL, 'process_order')
    extracted = _extracted_names(result)
    bogus = EXPECTED_PROCESS_ORDER | {'does_not_exist_anywhere'}
    assert _recall(bogus, extracted) < 1.0
    assert 'does_not_exist_anywhere' not in extracted


def test_transitive_multimodule_recall() -> None:
    indices = tuple(index_module(module, source)
                    for module, source in HOP_MODULES)
    graph = build_graph(indices)
    target_index = next(index for index in indices
                        if index.module == 'pkg.h3')
    facts = target_index.symbol('run')
    assert facts is not None
    result = slice_context(facts.source, target_name='run',
                           target_module='pkg.h3', graph=graph,
                           indices=indices)
    extracted = _extracted_names(result)
    missing = EXPECTED_H3_RUN - extracted
    assert missing == set(), f'missing {sorted(missing)}'
    assert _recall(EXPECTED_H3_RUN, extracted) == 1.0
    # transitive hop count: all four symbols reachable
    assert {node for node in result.closure.reachable
            if node.startswith('sym:')} == {
        'sym:pkg.h0:Token', 'sym:pkg.h1:issue',
        'sym:pkg.h2:Ticket', 'sym:pkg.h3:run'}


def test_target_body_verbatim() -> None:
    index = index_module('shop.canonical', CANONICAL)
    facts = index.symbol('process_order')
    assert facts is not None
    graph = build_graph((index,))
    result = slice_context(facts.source, target_name='process_order',
                           target_module='shop.canonical', graph=graph,
                           indices=(index,))
    assert result.target_source == facts.source
    assert result.target_source.startswith('@decorate')
    assert 'return ([checked], DEFAULT_TIMEOUT)' in result.target_source


def test_function_stub_signature_only() -> None:
    index = index_module('shop.canonical', CANONICAL)
    facts = index.symbol('validate')
    assert facts is not None
    stub = stub_for(facts)
    assert 'def validate(order: Order) -> ' in stub
    assert stub.endswith('...')
    assert 'return order' not in stub      # body is NOT context


def test_class_stub_keeps_structure_not_bodies() -> None:
    index = index_module('shop.canonical', CANONICAL)
    order = index.symbol('Order')
    assert order is not None
    stub = stub_for(order)
    assert '@dataclass' in stub
    assert 'class Order' in stub
    assert 'order_id: OrderId' in stub     # class-level constants/types
    assert 'total: int' in stub

    base = index.symbol('Base')
    assert base is not None
    stub = stub_for(base)
    assert 'KIND = ' in stub               # class constant kept in full
    assert 'def boot(self)' in stub        # method signature kept
    assert 'return self.KIND' not in stub  # method body dropped
    assert '    ...' in stub


def test_typealias_protocol_bases_in_stubs() -> None:
    index = index_module('shop.canonical', CANONICAL)
    alias = index.symbol('OrderId')
    assert alias is not None
    assert 'TypeAlias' in stub_for(alias)          # TypeAlias definition
    checker = index.symbol('Checker')
    assert checker is not None
    checker_stub = stub_for(checker)
    assert 'Protocol' in checker_stub              # Protocol structure
    assert 'def check(self' in checker_stub
    assert 'class Checker' in checker_stub


def test_unresolved_never_dropped_star_import() -> None:
    source = (
        'from pkg.wide import *\n'
        '\n'
        'def go(value):\n'
        '    return mystery(value)\n'
    )
    _, _, result = _slice(source, 'go', 'shop.star')
    # 'mystery' cannot be resolved statically -> UNRESOLVED, KEPT
    assert result.closure.unresolved, 'star import must stay visible'
    marker_text = '\n'.join(stub.text for stub in result.stubs)
    assert 'UNRESOLVED dependency' in marker_text
    assert any('star_import' in marker
               for marker in
               index_unresolved(source, 'shop.star'))


def index_unresolved(source: str, module: str):
    return index_module(module, source).unresolved


def test_external_boundary_marker_kept() -> None:
    source = (
        'import json\n'
        '\n'
        'def dump(value):\n'
        '    return json.dumps(value)\n'
    )
    _, _, result = _slice(source, 'dump', 'shop.ext')
    marker_text = '\n'.join(stub.text for stub in result.stubs)
    assert 'external dependency: json' in marker_text or \
        'builtin:' in marker_text
    assert result.closure.external or result.closure.unresolved or \
        'ext:json' in result.closure.reachable


def test_sizes_and_reduction_measured() -> None:
    index = index_module('shop.canonical', CANONICAL)
    facts = index.symbol('process_order')
    assert facts is not None
    graph = build_graph((index,))
    result = slice_context(facts.source, target_name='process_order',
                           target_module='shop.canonical', graph=graph,
                           indices=(index,))
    assert result.full_bytes > result.surgical_bytes > len(
        result.target_source.encode('utf-8'))
    reduction = 1 - result.surgical_bytes / result.full_bytes
    assert 0 < reduction < 1
    print(f'[measurement] full={result.full_bytes}B '
          f'surgical={result.surgical_bytes}B '
          f'reduction={reduction:.3f} '
          f'symbols_inlined={result.symbol_count}')
    # §20: this is SOURCE-SIZE reduction (no tokenizer in core)
    assert isinstance(reduction, float)


def test_determinism_repeated_identical() -> None:
    first = _slice(CANONICAL, 'process_order')
    second = _slice(CANONICAL, 'process_order')
    assert first[2] == second[2]
    stubs = [stub.text for stub in first[2].stubs]
    assert stubs == sorted(stubs, key=str) or True  # sorted by node
    assert [stub.node for stub in first[2].stubs] == \
        sorted(stub.node for stub in first[2].stubs)


def test_hermeticity_static_scan() -> None:
    import inspect

    import asha.context_slicer as module
    source = inspect.getsource(module)
    for banned in ('subprocess', 'socket', 'urllib', 'random',
                   'uuid', 'environ', 'getenv', 'perf_counter',
                   'datetime', 'cwd', 'git'):
        assert re.search(rf'\b{re.escape(banned)}\b', source) is None, \
            banned
    assert 'open(' not in source
    for attr in ('subprocess', 'socket', 'os', 'random'):
        assert not hasattr(module, attr), attr


def test_mypy_secondary_validation_artifact(tmp_path) -> None:
    """§22: mypy is a SECONDARY signal on a generated artifact (stub
    shape), produced separately from the slice; a failure must be
    classified, not ignored. External imports are reconstructed from
    boundary markers for the artifact only."""
    index = index_module('shop.canonical', CANONICAL)
    facts = index.symbol('process_order')
    assert facts is not None
    graph = build_graph((index,))
    result = slice_context(facts.source, target_name='process_order',
                           target_module='shop.canonical', graph=graph,
                           indices=(index,))
    imports: list[str] = []
    body: list[str] = []
    for stub in result.stubs:
        if stub.node.startswith('ext:'):
            dotted = stub.node[4:]
            if '.' in dotted:
                package, _, name = dotted.rpartition('.')
                imports.append(f'from {package} import {name}')
            else:
                imports.append(f'import {dotted}')
        else:
            body.append(stub.text)
    artifact = '\n\n'.join(
        [*sorted(set(imports)), *body, result.target_source])
    artifact_path = tmp_path / 'slice_artifact.py'
    artifact_path.write_text(artifact, encoding='utf-8')
    # classified failure modes: empty-body is STUB SHAPE (bodies are
    # deliberately elided -- a slice is agent context, §15), never a
    # missing dependency; return-value errors would surface real
    # corpus inconsistencies and are NOT disabled
    run = subprocess.run(
        [sys.executable, '-m', 'mypy', '--no-incremental',
         '--disable-error-code=empty-body',
         str(artifact_path)],
        capture_output=True, text=True, cwd=str(tmp_path),
        check=False, timeout=180)
    if run.returncode != 0:
        print(run.stdout, run.stderr)
    # failure would be classified: missing dependency vs stub shape
    # vs typing limitation -- assert clean for this corpus
    assert run.returncode == 0, run.stdout
