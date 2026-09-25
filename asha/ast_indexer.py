"""Phase 4.1 -- stdlib AST indexer: deterministic symbol facts.

Input is EXPLICIT source text (``index_module(name, source)``) -- the
indexer never discovers files, never executes code or imports, never
touches network/clock/environment. Anything that cannot be resolved
statically is recorded as UNRESOLVED, never dropped (a static analyzer,
not an interpreter: "cannot resolve" != "probably irrelevant").

Name resolution models the lexical scopes Python actually has (module,
class, function, nested function, comprehension, lambda) with
shadowing: a local binding satisfies inner loads, so a shadowed name
never leaks into module-level reads. Entering a function DROPS enclosing
class frames (methods do not see class scope -- CPython semantics), so
a class attribute never masks a module-level dependency of a method.

Free names of a top-level symbol (loads that escape its subtree) are
that symbol's dependencies -- including annotation names (mandatory),
decorator references, base classes, default-argument expressions and
comprehension references. Nested definitions contribute their free
names to the enclosing symbol as well (editing the outer body includes
its inner closures).
"""
from __future__ import annotations

import ast
import builtins
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

_BUILTIN_NAMES = frozenset(vars(builtins))


def is_builtin(name: str) -> bool:
    """Deterministic in-environment builtin test (stdlib only, no I/O)."""
    return name in _BUILTIN_NAMES


@dataclass(frozen=True)
class ImportFact:
    module: str
    kind: str                       # 'import' | 'from' | 'star'
    names: tuple[tuple[str, str], ...]  # (original, alias) alias='' if none
    level: int                      # relative-import level (0 = absolute)
    type_checking: bool
    conditional: bool


@dataclass(frozen=True)
class SymbolFacts:
    name: str                       # 'helper', 'Cls', 'Cls.method'
    kind: str                       # 'function' | 'method' | 'class' | 'variable'
    reads: frozenset[str]
    writes: frozenset[str]
    mutations: frozenset[str]       # attribute/subscript mutation bases
    annotations: frozenset[str]
    calls: frozenset[str]
    attributes: frozenset[str]      # dotted paths ('self.user.id')
    bases: frozenset[str]
    decorators: frozenset[str]
    local_names: frozenset[str]     # params + bound locals (intra-symbol)
    source: str                     # full original segment (stub material)


@dataclass(frozen=True)
class ModuleIndex:
    module: str
    symbols: tuple[SymbolFacts, ...]      # sorted by name (canonical)
    imports: tuple[ImportFact, ...]       # deterministic walk order
    module_writes: frozenset[str]         # module-level bound names
    unresolved: tuple[str, ...]           # dynamic/star markers, sorted

    def symbol(self, name: str) -> SymbolFacts | None:
        for fact in self.symbols:
            if fact.name == name:
                return fact
        return None

    def alias_table(self) -> dict[str, tuple[str, str, int]]:
        """bound alias -> (module, original-or-'', relative-level).

        Relative (level>0) from-imports are attributed TOO: the level
        travels with the binding and codegraph canonicalises the module
        path through the same import-target path as import edges
        (Phase 5.1 repair C -- the old level==0 filter was an
        artificial attribution gap that minted UNRESOLVED nodes).
        """
        table: dict[str, tuple[str, str, int]] = {}
        for fact in self.imports:
            if fact.kind == 'import':
                for original, alias in fact.names:
                    if alias:
                        table[alias] = (original, '', 0)
                    else:
                        top = original.split('.')[0]
                        table[top] = (top, '', 0)
            elif fact.kind == 'from':
                for original, alias in fact.names:
                    table[alias or original] = (
                        fact.module, original, fact.level)
        return table


class _Scope:
    __slots__ = ('bound', 'kind')

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.bound: set[str] = set()


class _Sink:
    """Per-symbol dependency sinks (plain sets, merged into facts)."""

    def __init__(self) -> None:
        self.reads: set[str] = set()
        self.writes: set[str] = set()
        self.mutations: set[str] = set()
        self.annotations: set[str] = set()
        self.calls: set[str] = set()
        self.attributes: set[str] = set()
        self.bases: set[str] = set()
        self.decorators: set[str] = set()
        self.local_names: set[str] = set()
        self.unresolved: list[str] = []


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return '.'.join(reversed(parts))
    return None


def _root_name(node: ast.AST) -> str | None:
    current: ast.AST = node
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return None


class _Analyzer:
    """Scope-aware walker producing one _Sink per definition."""

    def __init__(self, module_scope: _Scope) -> None:
        self._module_scope = module_scope
        self._scopes: list[_Scope] = [module_scope]
        self._path: list[str] = []
        self._facts: dict[str, SymbolFacts] = {}
        self._path_kinds: list[str] = []
        self._sink = _Sink()

    # ------------------------------------------------------ resolution
    def _push(self, kind: str) -> _Scope:
        frame = _Scope(kind)
        self._scopes.append(frame)
        return frame

    def _pop(self) -> None:
        self._scopes.pop()

    def _enter_function(self, kind: str) -> _Scope:
        # CPython: function scope skips enclosing class frames
        kept = [scope for scope in self._scopes if scope.kind != 'class']
        frame = _Scope(kind)
        self._scopes = [*kept, frame]
        return frame

    def _load(self, name: str, *, call: bool = False) -> None:
        for scope in reversed(self._scopes[1:]):
            if name in scope.bound:
                return                      # shadowed locally
        sink = self._sink
        if call:
            sink.calls.add(name)
        if name in self._module_scope.bound:
            sink.reads.add(name)            # module-level call/reference
            return
        sink.reads.add(name)                # free global reference

    def _bind(self, name: str) -> None:
        self._scopes[-1].bound.add(name)
        self._sink.local_names.add(name)

    # ----------------------------------------------------------- nodes
    def visit(self, node: ast.AST) -> None:
        method = 'visit_' + node.__class__.__name__
        visitor = getattr(self, method, None)
        if visitor is not None:
            visitor(node)
            return
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self._sink.writes.add(node.id)
            self._bind(node.id)
        elif isinstance(node.ctx, ast.Del):
            self._sink.writes.add(node.id)
        else:
            self._load(node.id)

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            self._sink.reads.add(name)
            self._sink.writes.add(name)
            self._module_scope.bound.add(name)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for name in node.names:
            # a nonlocal declaration binds the name LOCALLY (enclosing
            # function scope) -- never a module-level reference.
            self._bind(name)
            self._sink.writes.add(name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        # `except E as exc:` binds in the ENCLOSING scope for the
        # handler body -- provable lexical binding (Phase 5.1 repair A).
        if node.name:
            self._bind(node.name)
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        path = _dotted(node)
        if path:
            self._sink.attributes.add(path)
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            base = _root_name(node)
            if base:
                self._sink.mutations.add(base)
                self._load(base)
            # visit value's inner attributes for nested loads
            if isinstance(node.value, (ast.Attribute, ast.Subscript)):
                self.visit(node.value)
        else:
            for child in ast.iter_child_nodes(node):
                self.visit(child)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            base = _root_name(node)
            if base:
                self._sink.mutations.add(base)
                self._load(base)
            self.visit(node.slice)
        else:
            for child in ast.iter_child_nodes(node):
                self.visit(child)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            self._load(func.id, call=True)
            if func.id in ('__import__', 'exec', 'eval'):
                self._sink.unresolved.append(f'dynamic_import:{func.id}')
        elif isinstance(func, ast.Attribute):
            path = _dotted(func)
            if path:
                self._sink.attributes.add(path)
                self._sink.calls.add(path)
                root = _root_name(func)
                if root:
                    self._load(root)
                if path.rsplit('.', 1)[-1] == 'import_module':
                    self._sink.unresolved.append(
                        f'dynamic_import:{path}')
            else:
                # nested call in callee position: X().method() -- the
                # VALUE subtree carries dependencies (e.g. the class
                # being instantiated); must be visited or they are lost
                self.visit(func)
        else:
            self.visit(func)
        for child in ast.iter_child_nodes(node):
            if child is not func:
                self.visit(child)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        # variable annotations are TYPE-SPACE dependencies (mandatory)
        self._annotation(node.annotation)
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # x += 1 both READS and WRITES x (ctx is ambiguous across
        # versions -- handled explicitly for both)
        root = _root_name(node.target)
        if root:
            self._load(root)
            self._sink.writes.add(root)
        self.visit(node.target)
        self.visit(node.value)

    def _annotation(self, node: ast.AST | None) -> None:
        if node is None:
            return
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                self._sink.annotations.add(sub.id)
            elif isinstance(sub, ast.Constant) and \
                    isinstance(sub.value, str):
                # PEP-563 / quoted forward references: the string is a
                # TYPE-SPACE expression -- parse it and collect the
                # names it references (mandatory type-dependency
                # recall); invalid strings become UNRESOLVED, dropped
                # by nothing
                try:
                    parsed = ast.parse(sub.value, mode='eval')
                except SyntaxError:
                    self._sink.unresolved.append(
                        f'annotation_string:{sub.value[:40]}')
                else:
                    for nested in ast.walk(parsed):
                        if isinstance(nested, ast.Name):
                            self._sink.annotations.add(nested.id)
        self.visit(node)

    def _decorator(self, node: ast.AST) -> None:
        self.visit(node)
        if isinstance(node, ast.Name):
            self._sink.decorators.add(node.id)
        elif isinstance(node, ast.Attribute):
            path = _dotted(node)
            if path:
                self._sink.decorators.add(path)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        frame = self._enter_function('lambda')
        self._bind_args(node.args, frame)
        self.visit(node.body)
        self._pop_function(frame)

    def _pop_function(self, frame: _Scope) -> None:
        if frame in self._scopes:
            self._scopes.remove(frame)
        # restore any class frames dropped on entry: they are only
        # removed for the duration of this function (path-based restore)
        # -- since analysis of a definition happens in its own pass,
        # dropping is safe; nothing to restore here.

    def _bind_args(self, args: ast.arguments, frame: _Scope) -> None:
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            frame.bound.add(arg.arg)
            self._sink.local_names.add(arg.arg)
        if args.vararg:
            frame.bound.add(args.vararg.arg)
            self._sink.local_names.add(args.vararg.arg)
        if args.kwarg:
            frame.bound.add(args.kwarg.arg)
            self._sink.local_names.add(args.kwarg.arg)

    def _comprehension(self, generators: list[ast.comprehension],
                       body_nodes: list[ast.AST]) -> None:
        frame = self._push('comp')
        for index, generator in enumerate(generators):
            if index == 0:
                # first iterable evaluates in the ENCLOSING scope
                self._scopes.remove(frame)
                self.visit(generator.iter)
                self._scopes.append(frame)
            else:
                self.visit(generator.iter)
            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for node in body_nodes:
            self.visit(node)
        self._pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node.generators,
                            [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node.generators, [node.key, node.value])

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef
                  ) -> None:
        # outer context (decorators/defaults/annotations live in the
        # ENCLOSING scope and are dependencies of this definition)
        for decorator in node.decorator_list:
            self._decorator(decorator)
        args = node.args
        for default in [*args.defaults, *args.kw_defaults]:
            if default is not None:
                self.visit(default)
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            self._annotation(arg.annotation)
        if args.vararg:
            self._annotation(args.vararg.annotation)
        if args.kwarg:
            self._annotation(args.kwarg.annotation)
        self._annotation(node.returns)
        if len(self._scopes) > 1:
            # PEP 227: a nested `def` name binds in the ENCLOSING local
            # scope when the def statement executes (after decorators /
            # defaults / annotations above). Module-level defs are left
            # to module symbols. Loads before this point stay reads --
            # they are genuine forward-reference NameErrors.
            self._bind(node.name)

        # body in its own frame (class frames already dropped on entry)
        outer_sink, outer_scopes, outer_path, outer_kinds = (
            self._sink, self._scopes, list(self._path),
            list(self._path_kinds))
        qualified = '.'.join([*outer_path, node.name])
        self._sink = _Sink()
        frame = self._enter_function('function')
        self._bind_args(args, frame)
        self._path = [*outer_path, node.name]
        self._path_kinds = [*outer_kinds, 'function']
        for statement in node.body:
            self.visit(statement)
        child_sink = self._sink
        child_facts = dict(self._facts)
        # symbol fact = outer context (decorators/defaults/annotations)
        # UNION body free names -- recorded AFTER the body so no
        # dependency of the definition itself can be lost
        self._sink = _merge_sinks(outer_sink, child_sink)
        self._facts = child_facts
        is_method = bool(outer_kinds) and outer_kinds[-1] == 'class'
        self._record_definition(
            node, kind='method' if is_method else 'function',
            qualified=qualified)
        self._scopes = outer_scopes
        self._path = outer_path
        self._path_kinds = outer_kinds
        self._sink.unresolved = outer_sink.unresolved + child_sink.unresolved

    def _record_definition(
            self, node: ast.FunctionDef | ast.AsyncFunctionDef
            | ast.ClassDef, kind: str, qualified: str) -> None:
        name = qualified
        sink = self._sink
        self._facts[name] = SymbolFacts(
            name=name, kind=kind,
            reads=frozenset(sink.reads),
            writes=frozenset(sink.writes),
            mutations=frozenset(sink.mutations),
            annotations=frozenset(sink.annotations),
            calls=frozenset(sink.calls),
            attributes=frozenset(sink.attributes),
            bases=frozenset(sink.bases),
            decorators=frozenset(sink.decorators),
            local_names=frozenset(sink.local_names),
            source=ast.unparse(node),
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self._decorator(decorator)
        for base in node.bases:
            self.visit(base)
            if isinstance(base, ast.Name):
                self._sink.bases.add(base.id)
            elif isinstance(base, ast.Attribute):
                path = _dotted(base)
                if path:
                    self._sink.bases.add(path)
        for keyword in node.keywords:
            self.visit(keyword.value)

        outer_sink, outer_scopes, outer_path, outer_kinds = (
            self._sink, self._scopes, list(self._path),
            list(self._path_kinds))
        qualified = '.'.join([*outer_path, node.name])
        self._sink = _Sink()
        self._push('class')
        self._path = [*outer_path, node.name]
        self._path_kinds = [*outer_kinds, 'class']
        for statement in node.body:
            self.visit(statement)
        if len(outer_scopes) > 1:
            # PEP 227: a nested `class` name binds in the ENCLOSING local
            # scope AFTER the class body executes -- in-body self-references
            # remain reads (they are real NameErrors at runtime).
            outer_scopes[-1].bound.add(node.name)
            outer_sink.local_names.add(node.name)
        child_sink = self._sink
        child_facts = dict(self._facts)
        # class fact = outer context (decorators/bases) UNION class-body
        # free names -- recorded AFTER the body, from the merged sink
        self._sink = _merge_sinks(outer_sink, child_sink)
        self._facts = child_facts
        self._record_class(node, qualified)
        self._scopes = outer_scopes
        self._path = outer_path
        self._path_kinds = outer_kinds
        self._sink.unresolved = outer_sink.unresolved + child_sink.unresolved

    def _record_class(self, node: ast.ClassDef,
                      qualified: str) -> None:
        name = qualified
        sink = self._sink
        self._facts[name] = SymbolFacts(
            name=name, kind='class',
            reads=frozenset(sink.reads),
            writes=frozenset(sink.writes),
            mutations=frozenset(sink.mutations),
            annotations=frozenset(sink.annotations),
            calls=frozenset(sink.calls),
            attributes=frozenset(sink.attributes),
            bases=frozenset(sink.bases),
            decorators=frozenset(sink.decorators),
            local_names=frozenset(sink.local_names),
            source=ast.unparse(node),
        )


def _merge_sinks(first: _Sink, second: _Sink) -> _Sink:
    merged = _Sink()
    merged.reads = first.reads | second.reads
    merged.writes = first.writes | second.writes
    merged.mutations = first.mutations | second.mutations
    merged.annotations = first.annotations | second.annotations
    merged.calls = first.calls | second.calls
    merged.attributes = first.attributes | second.attributes
    merged.bases = first.bases | second.bases
    merged.decorators = first.decorators | second.decorators
    merged.local_names = first.local_names | second.local_names
    merged.unresolved = first.unresolved + second.unresolved
    return merged


# ----------------------------------------------------------- imports
def _collect_imports(tree: ast.Module) -> tuple[list[ImportFact],
                                                 list[str]]:
    facts: list[ImportFact] = []
    unresolved: list[str] = []

    def walk(statements: list[ast.stmt], *, type_checking: bool,
             conditional: bool) -> None:
        for statement in statements:
            if isinstance(statement, ast.Import):
                names = tuple(sorted(
                    (alias.name, alias.asname or '')
                    for alias in statement.names))
                facts.append(ImportFact(
                    module=names[0][0] if names else '', kind='import',
                    names=names, level=0, type_checking=type_checking,
                    conditional=conditional))
            elif isinstance(statement, ast.ImportFrom):
                if any(alias.name == '*' for alias in statement.names):
                    facts.append(ImportFact(
                        module=statement.module or '', kind='star',
                        names=(), level=statement.level,
                        type_checking=type_checking,
                        conditional=conditional))
                    unresolved.append(
                        f"star_import:{statement.module or '.'}")
                else:
                    names = tuple(sorted(
                        (alias.name, alias.asname or '')
                        for alias in statement.names))
                    facts.append(ImportFact(
                        module=statement.module or '', kind='from',
                        names=names, level=statement.level,
                        type_checking=type_checking,
                        conditional=conditional))
            elif isinstance(statement, ast.If):
                inner_tc = type_checking or _is_type_checking(
                    statement.test)
                inner_cond = conditional or not inner_tc
                walk(statement.body, type_checking=inner_tc,
                     conditional=inner_cond)
                walk(statement.orelse, type_checking=type_checking,
                     conditional=True)
            elif isinstance(statement, ast.Try):
                groups: list[list[ast.stmt]] = [statement.body]
                for handler in statement.handlers:
                    groups.append(handler.body)
                groups.append(statement.orelse)
                groups.append(statement.finalbody)
                for group in groups:
                    walk(group, type_checking=type_checking,
                         conditional=True)
            elif isinstance(statement, (ast.For, ast.While)):
                walk(statement.body, type_checking=type_checking,
                     conditional=True)
                walk(statement.orelse, type_checking=type_checking,
                     conditional=True)
            elif isinstance(statement, (ast.With, ast.AsyncWith,
                                        ast.FunctionDef,
                                        ast.AsyncFunctionDef,
                                        ast.ClassDef)):
                walk(statement.body, type_checking=type_checking,
                     conditional=True)
            elif isinstance(statement, ast.Match):
                for case in statement.cases:
                    walk(case.body, type_checking=type_checking,
                         conditional=True)

    walk(tree.body, type_checking=False, conditional=False)
    return facts, unresolved


def _is_type_checking(test: ast.AST) -> bool:
    if isinstance(test, ast.Name):
        return test.id == 'TYPE_CHECKING'
    if isinstance(test, ast.Attribute):
        return _dotted(test) in ('typing.TYPE_CHECKING',
                                 't.TYPE_CHECKING')
    return False


def _dynamic_markers(tree: ast.Module) -> list[str]:
    markers: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in (
                '__import__', 'exec', 'eval'):
            markers.append(f'dynamic_import:{func.id}')
        elif isinstance(func, ast.Attribute):
            path = _dotted(func)
            if path is not None and \
                    path.rsplit('.', 1)[-1] == 'import_module':
                markers.append(f'dynamic_import:{path}')
    return sorted(set(markers))


_SESSION_CACHE: dict[tuple[str, str], ModuleIndex] = {}
_SESSION_CACHE_LIMIT = 8192


def _session_key(module: str, source: str) -> tuple[str, str]:
    # content-addressed: a stale entry is structurally impossible
    return (module, hashlib.sha256(source.encode('utf-8')).hexdigest())


def _remember(module: str, source: str, index: ModuleIndex) -> None:
    if len(_SESSION_CACHE) >= _SESSION_CACHE_LIMIT:
        _SESSION_CACHE.pop(next(iter(_SESSION_CACHE)))  # FIFO eviction
    _SESSION_CACHE[_session_key(module, source)] = index


def prime_session_cache(
        entries: Iterable[tuple[str, str, ModuleIndex]]) -> None:
    """Integration layer (Phase 5.1 Step 3): seed the in-process memo
    with already-constructed indices (disk-cache assemble). Pure
    memoization of deterministic parses -- zero governance authority:
    no verdicts, no eligibility, no policy state is stored here."""
    for module, source, index in entries:
        _remember(module, source, index)


def clear_session_cache() -> None:
    """Drop the memo (test isolation / explicit cold path)."""
    _SESSION_CACHE.clear()


def index_module(module: str, source: str) -> ModuleIndex:
    """Parse ``source`` and extract deterministic module facts.

    Session memo: an identical (module, source) pair returns the
    already-built index (frozen dataclass -- sharing is safe) and the
    content-addressed key makes a stale hit impossible. Misses and
    failures parse exactly as before.

    Raises ValueError on unparseable source (fail-closed API boundary;
    callers treat it as UNKNOWN, never as 'no dependencies').
    """
    cached = _SESSION_CACHE.get(_session_key(module, source))
    if cached is not None:
        return cached
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(
            f'ast_indexer: unparseable source for {module}: {exc.msg}'
        ) from None

    imports, import_unresolved = _collect_imports(tree)

    module_scope = _Scope('module')
    module_bindings: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
            module_bindings.add(statement.name)
            module_scope.bound.add(statement.name)
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                if alias.name == '*':
                    continue
                if isinstance(statement, ast.ImportFrom):
                    bound = alias.asname or alias.name
                else:
                    bound = alias.asname or alias.name.split('.')[0]
                module_bindings.add(bound)
                module_scope.bound.add(bound)
        else:
            for name in _store_names(statement):
                module_bindings.add(name)
                module_scope.bound.add(name)

    analyzer = _Analyzer(module_scope)
    variable_sinks: dict[str, _Sink] = {}
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
            analyzer._sink = _Sink()
            analyzer._scopes = [module_scope]
            analyzer._path = []
            analyzer.visit(statement)
        elif not isinstance(statement, (ast.Import, ast.ImportFrom)):
            # module-level expressions: bind targets, record variables
            # (imports are bindings, never variable symbols)
            targets = _store_names(statement)
            sink = _Sink()
            analyzer._sink = sink
            analyzer._scopes = [module_scope]
            analyzer._path = []
            analyzer.visit(statement)
            for target in targets:
                variable_sinks[target] = sink

    facts = dict(analyzer._facts)
    for name in sorted(variable_sinks):
        sink = variable_sinks[name]
        node_source = _statement_source(tree, name) or name
        facts[name] = SymbolFacts(
            name=name, kind='variable',
            reads=frozenset(sink.reads),
            writes=frozenset(sink.writes - {name}),
            mutations=frozenset(sink.mutations),
            annotations=frozenset(sink.annotations),
            calls=frozenset(sink.calls),
            attributes=frozenset(sink.attributes),
            bases=frozenset(), decorators=frozenset(),
            local_names=frozenset(sink.local_names),
            source=node_source,
        )

    unresolved = sorted(set(import_unresolved) | set(_dynamic_markers(tree))
                        | set(analyzer._sink.unresolved))
    ordered = tuple(facts[name] for name in sorted(facts))
    index = ModuleIndex(
        module=module,
        symbols=ordered,
        imports=tuple(imports),
        module_writes=frozenset(module_bindings),
        unresolved=tuple(unresolved),
    )
    _remember(module, source, index)
    return index


def _store_names(statement: ast.stmt) -> tuple[str, ...]:
    names: list[str] = []

    def collect(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                collect(element)
        elif isinstance(target, ast.Starred):
            collect(target.value)
        elif isinstance(target, ast.Attribute):
            root = _root_name(target)
            if root:
                names.append(root)

    if isinstance(statement, ast.Assign):
        for target in statement.targets:
            collect(target)
    elif isinstance(statement, (ast.AnnAssign, ast.AugAssign,
                               ast.For, ast.AsyncFor)):
        collect(statement.target)
    elif isinstance(statement, (ast.With, ast.AsyncWith)):
        for item in statement.items:
            if item.optional_vars is not None:
                collect(item.optional_vars)
    elif isinstance(statement, (ast.Import, ast.ImportFrom)):
        for alias in statement.names:
            if alias.name == '*':
                continue
            if isinstance(statement, ast.ImportFrom):
                names.append(alias.asname or alias.name)
            else:
                names.append(alias.asname or alias.name.split('.')[0])
    return tuple(dict.fromkeys(names))


def _statement_source(tree: ast.Module, name: str) -> str | None:
    for statement in tree.body:
        if name in _store_names(statement):
            return ast.unparse(statement)
    return None
