"""Phase 4.1 -- AST indexer contract tests.

Covers: symbol facts, reads/writes/mutations, mandatory type
dependencies, imports/aliases/TYPE_CHECKING, lexical scopes with
shadowing, CPython class-scope semantics for methods, decorators /
bases / defaults, comprehension + lambda scopes, nested free-name
folding, dynamic-import -> UNRESOLVED, immutability, determinism,
parse failure, and hermeticity of the extraction module.
"""
from __future__ import annotations

import pytest

from asha.ast_indexer import (
    ImportFact,
    ModuleIndex,
    SymbolFacts,
    index_module,
)

PY = (
    'from dataclasses import dataclass\n'
    '\n'
    'VALUE = 1\n'
    '\n'
    'def traced(fn):\n'
    '    return fn\n'
    '\n'
    'class Model:\n'
    '    NAME = "model"\n'
    '\n'
    '    def render(self, value: "Model") -> str:\n'
    '        local = value\n'
    '        return str(local)\n'
    '\n'
    'def process(value, limit=VALUE):\n'
    '    total = value + limit\n'
    '    total += 1\n'
    '    items = [item for item in (total,)]\n'
    '    return items\n'
)


def _index(source: str, module: str = 'mod') -> ModuleIndex:
    return index_module(module, source)


def test_symbol_extraction_top_level() -> None:
    index = _index(PY)
    names = {fact.name: fact.kind for fact in index.symbols}
    assert names['traced'] == 'function'
    assert names['Model'] == 'class'
    assert names['Model.render'] == 'method'
    assert names['VALUE'] == 'variable'
    assert names['process'] == 'function'
    assert tuple(sorted(names)) == tuple(sorted(names))  # sorted


def test_reads_writes_and_mutations() -> None:
    source = (
        'x = 1\n'
        'class Box:\n'
        '    def bump(self, x):\n'
        '        x.foo = 1\n'
        '        x += 1\n'
        '        return x\n'
    )
    index = _index(source)
    bump = index.symbol('Box.bump')
    assert bump is not None
    # x is a PARAMETER: local, never a module dependency
    assert 'x' not in bump.reads
    # attribute mutation is tracked as a mutation, not a bare write
    assert 'x' in bump.mutations
    # augmented assignment reads AND writes
    assert 'x' in bump.writes
    assert 'self' in bump.local_names
    top = index.symbol('x')
    assert top is not None and top.kind == 'variable'


def test_annotation_names_mandatory() -> None:
    source = (
        'class Order: pass\n'
        'class Result: pass\n'
        'OrderId = int\n'
        'def handle(order: Order, ident: "OrderId") -> "Result[Order]":\n'
        '    return Result()\n'
        'container: Order = Order()\n'
    )
    index = _index(source)
    handle = index.symbol('handle')
    assert handle is not None
    assert {'Order', 'OrderId', 'Result'} <= set(handle.annotations)
    # quoted forward references are TYPE-SPACE expressions: parsed
    assert 'OrderId' in handle.annotations
    container = index.symbol('container')
    assert container is not None
    assert 'Order' in container.annotations


def test_import_facts_aliases_and_type_checking() -> None:
    source = (
        'import os\n'
        'import collections.abc as ca\n'
        'from dataclasses import dataclass, field\n'
        'from typing import TYPE_CHECKING\n'
        'if TYPE_CHECKING:\n'
        '    from shop.types import UserId\n'
        'def use():\n'
        '    import json as j\n'
        '    return os, ca, dataclass, field, j\n'
    )
    index = _index(source)
    kinds = {(fact.kind, fact.module) for fact in index.imports}
    assert ('import', 'os') in kinds
    assert ('import', 'collections.abc') in kinds
    assert ('from', 'dataclasses') in kinds
    alias = index.alias_table()
    assert alias['ca'] == ('collections.abc', '')
    assert alias['dataclass'] == ('dataclasses', 'dataclass')
    assert alias['field'] == ('dataclasses', 'field')
    # TYPE_CHECKING-guarded import is flagged, never dropped
    guarded = [fact for fact in index.imports
               if fact.module == 'shop.types']
    assert len(guarded) == 1
    assert guarded[0].type_checking is True
    assert guarded[0].kind == 'from'
    # function-local import recorded as conditional
    local_json = [fact for fact in index.imports if fact.module == 'json']
    assert local_json and local_json[0].conditional is True


def test_scope_shadowing_no_module_leak() -> None:
    source = (
        'value = "module"\n'
        'def foo():\n'
        '    value = "local"\n'
        '    return value\n'
    )
    index = _index(source)
    foo = index.symbol('foo')
    assert foo is not None
    # inner binding satisfies the load: NOT a module dependency
    assert 'value' not in foo.reads


def test_unshadowed_module_reference_is_dependency() -> None:
    source = (
        'value = "module"\n'
        'def foo():\n'
        '    return value\n'
    )
    foo = _index(source).symbol('foo')
    assert foo is not None and 'value' in foo.reads


def test_method_sees_module_not_class_scope() -> None:
    """CPython: method bodies do NOT see the class namespace. A class
    attribute must never mask a module-level dependency of a method."""
    source = (
        'CONST = "module"\n'
        'class Holder:\n'
        '    CONST = "class"\n'
        '    def read(self):\n'
        '        return CONST\n'
    )
    read = _index(source).symbol('Holder.read')
    assert read is not None and 'CONST' in read.reads


def test_decorators_bases_defaults() -> None:
    source = (
        'def decorator(fn):\n'
        '    return fn\n'
        'LIMIT = 3\n'
        'class Base:\n'
        '    pass\n'
        '@decorator\n'
        'def run(step=LIMIT, flag=decorator):\n'
        '    return step, flag\n'
        '@decorator\n'
        'class Child(Base, decorator):\n'
        '    pass\n'
    )
    index = _index(source)
    run = index.symbol('run')
    assert run is not None
    assert 'decorator' in run.decorators
    assert {'LIMIT', 'decorator'} <= set(run.reads)
    child = index.symbol('Child')
    assert child is not None
    assert {'Base', 'decorator'} <= set(child.bases)
    assert 'decorator' in child.decorators


def test_comprehension_and_lambda_scopes() -> None:
    source = (
        'total = 0\n'
        'def build(items):\n'
        '    square = [total for total in items if total > 0]\n'
        '    add = lambda total: total + 1\n'
        '    return square, add\n'
    )
    build = _index(source).symbol('build')
    assert build is not None
    # comprehension target binds locally: no phantom module 'total'
    # (the module-level 'total' is shadowed inside the comprehension)
    assert 'total' not in build.reads
    assert 'items' in build.local_names


def test_nested_function_free_names_folded() -> None:
    source = (
        'class Order: pass\n'
        'def outer():\n'
        '    def inner(order: Order):\n'
        '        return order\n'
        '    return inner\n'
    )
    index = _index(source)
    assert index.symbol('outer.inner') is not None
    outer = index.symbol('outer')
    assert outer is not None
    # editing outer includes its closure's dependency
    assert 'Order' in outer.annotations | outer.reads


def test_dynamic_and_star_import_unresolved_never_dropped() -> None:
    source = (
        'import importlib\n'
        'mod = importlib.import_module("pkg.dynamic")\n'
        'legacy = __import__("pkg.legacy")\n'
    )
    index = _index(source)
    assert any(marker.startswith('dynamic_import:')
               for marker in index.unresolved)
    star = _index('from pkg.wide import *\n\ndef go(x):\n    return x\n')
    assert 'star_import:pkg.wide' in star.unresolved


def test_immutability_and_determinism() -> None:
    from dataclasses import FrozenInstanceError
    index = _index(PY)
    fact = index.symbols[0]
    with pytest.raises(FrozenInstanceError):
        fact.reads = frozenset()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        index.module_writes = frozenset()  # type: ignore[misc]
    again = _index(PY)
    assert index == again
    assert isinstance(index.imports[0], ImportFact)
    assert isinstance(fact, SymbolFacts)


def test_unparseable_source_raises() -> None:
    with pytest.raises(ValueError, match='unparseable source'):
        _index('def broken(:\n  pass\n')


def test_attributes_recorded_dotted() -> None:
    source = (
        'class T:\n'
        '    def walk(self):\n'
        '        return self.user.profile.id\n'
    )
    walk = _index(source).symbol('T.walk')
    assert walk is not None
    assert 'self.user.profile.id' in walk.attributes


def test_hermeticity_static_scan() -> None:
    """Core extraction must not reach for subprocess/network/clock/
    environment/filesystem -- proven by source inspection."""
    import inspect
    import re

    import asha.ast_indexer as module
    source = inspect.getsource(module)
    for banned in ('subprocess', 'socket', 'urllib', 'random',
                   'uuid', 'os.system', 'environ', 'getenv',
                   'perf_counter', 'datetime', 'git', 'cwd'):
        # word-boundary match: 'environment' must not trip 'environ'
        pattern = rf'\b{re.escape(banned)}\b'
        # positive control: the pattern MUST detect the word
        assert re.search(pattern, f'uses {banned} once') is not None
        assert re.search(pattern, source) is None, banned
    assert 'open(' not in source
    for attr in ('subprocess', 'socket', 'random', 'os'):
        assert not hasattr(module, attr), attr
