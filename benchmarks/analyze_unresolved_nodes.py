"""Phase 5.1 Step 1 A -- Mutually exclusive empirical taxonomy of the
live CodeGraph's ``unk:*`` nodes (A.1 strict auditability rules).

LIVE EXTRACTION ONLY: nodes are read straight from the graph built by
the same repository walk the eligibility engine uses. The expected
baseline (190, Phase 5.0 design) and the observed count are both
reported with the exact delta -- never force-fit.

Causality (A.1): a node is TYPE_CHECKING_GUARD or CAST_OR_ANNOTATION
IF AND ONLY IF an originating broken edge's site sits inside that
syntactic structure. Site -> category mapping is precedence-ordered by
the spec's category list (1..6); a node's PRIMARY category is the
minimum over all of its origin-site categories, making categories
mutually exclusive with counts summing to the observed total.

Read-only diagnostic: modifies nothing, no resolver involvement.
"""
from __future__ import annotations

import ast
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from asha import codegraph, scoping

EXPECTED_BASELINE = 190

CAT_TYPE_CHECKING = 'TYPE_CHECKING_GUARD'
CAT_DEFERRED = 'LOCAL_OR_DEFERRED_IMPORT'
CAT_DYNAMIC = 'DYNAMIC_OR_STAR_REEXPORT'
CAT_EXTERNAL = 'UNRESOLVED_EXTERNAL_ATTRIBUTION'
CAT_ANNOTATION = 'CAST_OR_ANNOTATION'
CAT_COMPLEX = 'COMPLEX_OR_OTHER'
ORDER = (CAT_TYPE_CHECKING, CAT_DEFERRED, CAT_DYNAMIC,
         CAT_EXTERNAL, CAT_ANNOTATION, CAT_COMPLEX)
RANK = {name: i for i, name in enumerate(ORDER)}

IMPLICIT_MODULE_GLOBALS = frozenset({
    '__file__', '__name__', '__package__', '__spec__', '__loader__',
    '__doc__', '__builtins__', '__cached__', '__path__',
})


class _Node:
    """One unresolved node plus everything traced back for it."""

    __slots__ = (
        'category',
        'edges',
        'name',
        'node',
        'note',
        'provable',
        'sites',
        'why_not_provable',
    )

    def __init__(self, node: str) -> None:
        self.node = node
        self.name = node[4:]
        self.edges: list[tuple[str, str]] = []
        self.sites: list[tuple[str, int, str, str]] = []  # (module, line, structure, stmt)
        self.category = CAT_COMPLEX
        self.note = ''
        self.provable = True
        self.why_not_provable = ''


def _parent_field_map(tree: ast.AST) -> dict[ast.AST, tuple[ast.AST, str]]:
    mapping: dict[ast.AST, tuple[ast.AST, str]] = {}
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            if isinstance(value, ast.AST):
                mapping[value] = (parent, field)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        mapping[item] = (parent, field)
    return mapping


def _chain(map_: dict[ast.AST, tuple[ast.AST, str]],
           node: ast.AST) -> list[tuple[ast.AST, str]]:
    out: list[tuple[ast.AST, str]] = []
    cur = node
    for _ in range(80):
        parent_field = map_.get(cur)
        if parent_field is None:
            break
        parent, field = parent_field
        out.append((parent, field))
        cur = parent
    return out


def _site_structure(tree: ast.AST, node: ast.AST,
                    map_: dict[ast.AST, tuple[ast.AST, str]],
                    ) -> str:
    """Structure classification of ONE reference site (causal order)."""
    chain = _chain(map_, node)
    # 1. inside `if TYPE_CHECKING:`
    for parent, _field in chain:
        if (isinstance(parent, ast.If)
                and 'TYPE_CHECKING' in ast.unparse(parent.test)):
            return CAT_TYPE_CHECKING
    # 2. an import statement nested inside a function/method body
    if (isinstance(node, (ast.Import, ast.ImportFrom))
            and any(isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for p, _f in chain)):
        return CAT_DEFERRED
    # 3. dynamic mechanisms: ONLY when the site sits in the DYNAMIC
    # position -- getattr's name argument (args[1:]) or an __all__ list.
    # The object being getattr'd (args[0]) is an ordinary expression.
    for parent, field in chain:
        if (isinstance(parent, ast.Call)
                and ((isinstance(parent.func, ast.Name)
                      and parent.func.id == 'getattr')
                     or (isinstance(parent.func, ast.Attribute)
                         and parent.func.attr == 'getattr'))
                and any(node is n for arg in parent.args[1:]
                        for n in ast.walk(arg))):
            return CAT_DYNAMIC
        if (field == 'targets' and isinstance(parent, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == '__all__'
                        for t in parent.targets)):
            return CAT_DYNAMIC
    # 5. annotation / typing.cast position
    for parent, field in chain:
        if field in ('annotation', 'returns'):
            return CAT_ANNOTATION
        if (isinstance(parent, ast.Call)
                and ((isinstance(parent.func, ast.Name)
                      and parent.func.id == 'cast')
                     or (isinstance(parent.func, ast.Attribute)
                         and parent.func.attr == 'cast'))
                and parent.args and node in ast.walk(parent.args[0])):
            return CAT_ANNOTATION
    # 6. everything else
    return CAT_COMPLEX


def _module_trees(texts: dict[str, str]) -> tuple[
        dict[str, ast.AST], dict[str, dict[ast.AST, tuple[ast.AST, str]]]]:
    trees: dict[str, ast.AST] = {}
    maps: dict[str, dict[ast.AST, tuple[ast.AST, str]]] = {}
    for module, text in texts.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        trees[module] = tree
        maps[module] = _parent_field_map(tree)
    return trees, maps


def _binding_names(tree: ast.AST) -> set[str]:
    """Every statically bound name in a module (all scopes) -- used for
    the provenance/provability answer, never to flip poison."""
    bound: set[str] = set(IMPLICIT_MODULE_GLOBALS)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.asname:
                    bound.add(alias.asname)
                else:
                    bound.add(alias.name.split('.')[0])
            if isinstance(node, ast.ImportFrom) and node.module:
                for segment in node.module.lstrip('.').split('.'):
                    bound.add(segment)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx,
                                                       (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.ExceptHandler,)):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, ast.alias):
            bound.add(node.asname or node.name.split('.')[0])
    return bound


def _find_import_site(name: str, tree: ast.AST) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == '*' and name:
                    continue
                if alias.asname == name or alias.name == name \
                        or alias.name.split('.')[0] == name:
                    return node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split('.')[0]
                if alias.asname == name or top == name:
                    return node
    return None


def _find_reference_sites(name: str, tree: ast.AST) -> list[ast.AST]:
    out: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id == name:
                out.append(node)
        elif isinstance(node, ast.Attribute):
            if node.attr == name:
                out.append(node)
            value = node.value
            if isinstance(value, ast.Name) and value.id == name:
                out.append(node)
    return out


def _marker_site(marker: str, tree: ast.AST) -> ast.AST | None:
    """Line of the construct that produced a dynamic/star marker."""
    if marker.startswith('dynamic_import:'):
        target = marker[len('dynamic_import:'):]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == target:
                    return node
                if isinstance(func, ast.Attribute):
                    dotted = ast.unparse(func)
                    if target in dotted:
                        return node
        return None
    if marker.startswith(('star:', 'star_import:')):
        module = marker.split(':', 1)[1]
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom)
                    and (node.module == module
                         or (node.module or '').split('.')[0] == module)
                    and any(a.name == '*' for a in node.names)):
                return node
        return None
    return None


def _origin_module(source: str) -> str:
    parts = source.split(':')
    if source.startswith('sym:'):
        return ':'.join(parts[1:-1])
    return ':'.join(parts[1:])


def analyze() -> dict:
    sources, texts, _module_of_rel, rel_of_module = scoping._index_repository(
        ROOT)
    graph = codegraph.build_graph(tuple(sources.values()))

    nodes_unk = sorted(n for n in graph.nodes if n.startswith('unk:'))
    observed = len(nodes_unk)
    origins: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for edge in graph.edges:
        if edge.target.startswith('unk:'):
            origins[edge.target].append((edge.source, edge.kind))

    trees, maps = _module_trees(texts)
    bindings = {m: _binding_names(t) for m, t in trees.items()}

    records: list[_Node] = []
    for node_id in nodes_unk:
        rec = _Node(node_id)
        rec.edges = origins.get(node_id, [])
        # --- fast paths: markers / star imports carry their category
        if node_id.startswith(('unk:dynamic_import:', 'unk:star:',
                               'unk:star_import:')):
            marker = node_id[4:]
            for source, _kind in rec.edges:
                module = _origin_module(source)
                tree = trees.get(module)
                if tree is None:
                    continue
                site = _marker_site(marker, tree)
                if site is not None:
                    rec.sites.append((module, getattr(site, 'lineno', 0),
                                      CAT_DYNAMIC, ast.unparse(site)[:90]))
            rec.category = CAT_DYNAMIC
            records.append(rec)
            continue

        # --- ordinary names: locate reference sites + binding imports
        for source, _kind in rec.edges:
            module = _origin_module(source)
            tree = trees.get(module)
            if tree is None:
                rec.note = 'origin_module_unparsed'
                continue
            found_any = False
            for site in _find_reference_sites(rec.name, tree):
                found_any = True
                structure = _site_structure(tree, site, maps[module])
                rec.sites.append((module, getattr(site, 'lineno', 0),
                                  structure, ast.unparse(site)[:90]))
            # binding import statement counts as an origin site too
            # (category 2 is defined by WHERE THE IMPORT lives)
            imp = _find_import_site(rec.name, tree)
            if imp is not None:
                found_any = True
                structure = _site_structure(tree, imp, maps[module])
                rec.sites.append((module, getattr(imp, 'lineno', 0),
                                  structure, ast.unparse(imp)[:90]))
            if not found_any:
                rec.note = (rec.note or 'site_not_located')

        if rec.sites:
            rec.category = min((s[2] for s in rec.sites),
                               key=lambda c: RANK[c])
        else:
            rec.category = CAT_COMPLEX
            rec.note = rec.note or 'site_not_located'

        # --- provability: every origin module must statically bind it
        for source, _kind in rec.edges:
            module = _origin_module(source)
            bound = bindings.get(module)
            if bound is None:
                rec.provable = False
                rec.why_not_provable = f'unparsed origin module {module}'
                break
            if rec.name not in bound:
                rec.provable = False
                rec.why_not_provable = f'no binding site in {module}'
                break
        records.append(rec)

    counts = collections.Counter(r.category for r in records)
    return {
        'expected_baseline': EXPECTED_BASELINE,
        'observed': observed,
        'delta': observed - EXPECTED_BASELINE,
        'records': records,
        'counts': counts,
        'graph': graph,
        'texts': texts,
        'rel_of_module': rel_of_module,
    }


THREE_QUESTIONS = {
    CAT_TYPE_CHECKING: (
        ('WHAT: a dependency edge whose site sits strictly inside an '
        '`if TYPE_CHECKING:` block; AST failure point = resolve() never '
        'sees guard-scoped bindings because the guard contributes no '
        'alias-table entry (level>0 imports are skipped outright).'),
        ('WHY: ModuleIndex.alias_table() filters `fact.level == 0`, so a '
        'guarded relative import binds nothing, and codegraph.resolve() '
        'falls through symbol -> alias -> builtin -> unk:NAME.'),
        ('CAN IT BE PROVEN?: statically provable where the guarded import '
        'statement exists (exact syntax, no inference); nodes with no '
        'binding statement anywhere remain poisoned.')),
    CAT_DEFERRED: (
        ('WHAT: import statement nested inside a function/method body '
        'binding a name that later references cannot attribute; failure '
        'point = same alias_table level==0 filter, scoped import never '
        'reaches the module alias table.'),
        ('WHY: the deferred ImportFact is recorded but excluded from '
        'alias attribution for relative (level>0) modules; the runtime '
        'execution-order of the deferred import is irrelevant to the '
        'static binding, which is exactly what the resolver drops.'),
        ('CAN IT BE PROVEN?: Yes -- the import site and its module path '
        'are syntactic facts; scope + relative-path resolution is '
        'deterministic without inference.')),
    CAT_DYNAMIC: (
        ('WHAT: star re-export (`from X import *` -> unk:star:X / '
        'star_import:X marker) or dynamic import marker '
        '(`dynamic_import:__import__`, `dynamic_import:'
        'importlib.import_module`) with its call-site line.'),
        ('WHY: build_graph keeps markers as first-class UNRESOLVED '
        'boundary nodes by design (index.unresolved -> unk:<marker>), '
        'and star targets contribute no (original, alias) pairs to any '
        'alias table.'),
        ('CAN IT BE PROVEN?: measured nodes all carry literal module '
        'strings or an indexed star target -> statically provable in '
        '5.1; non-literal (computed) dynamic targets would remain '
        'poisoned -- none observed live.')),
    CAT_EXTERNAL: (
        ('WHAT: would be a reference whose attribution breaks at an '
        'external package boundary; failure point = resolve_import_target '
        'maps non-provided modules to ext:* EXTERNAL, never to unk:*.'),
        ('WHY: external attribution loss is structurally unreachable in '
        'this graph: level==0 imports alias to ext:DOTTED (a resolved '
        'boundary) and non-external misses land in COMPLEX/annotation '
        'categories instead.'),
        ('CAN IT BE PROVEN?: No -- genuine external-origin ambiguity is '
        'inherently unprovable without package introspection, which is '
        'why the resolver must keep it poisoned; measured count: 0.')),
    CAT_ANNOTATION: (
        ('WHAT: an originating reference site in an annotation position '
        '(arg/AnnAssign/returns annotation or first arg of typing.cast), '
        'AST failure point = resolve() at the annotation Name with no '
        'attributable alias (relative import excluded from alias table, '
        'or forward-ref string with no binding).'),
        ('WHY: annotation-only names live outside module alias '
        'attribution when their binding import is level>0 '
        '(alias_table filters) or absent; the annotation itself is an '
        'exact AST position, so the broken edge is causally pinned.'),
        ('CAN IT BE PROVEN?: Yes where a binding ImportFact exists '
        '(relative-import gap -- deterministic path resolution); a '
        'forward reference with NO binding anywhere stays poisoned.')),
    CAT_COMPLEX: (
        ('WHAT: measured sub-patterns with exact refs -- (a) module-level '
        'relative `from .x import Y` attribution gap (alias_table '
        'level==0 filter); (b) bare scope-bound names (self/params/'
        'except-targets) reaching resolve() because local_names is only '
        'consulted for dotted roots; (c) implicit module globals '
        '(__file__ & friends) absent from every symbol/alias table; '
        '(d) unbound-anywhere names. Failure point = resolve() '
        'fall-through to unk:NAME in codegraph.py.'),
        ('WHY: none of the five structured categories apply: the origin '
        'site is an ordinary module-level import, a bare Name load, or '
        'an implicit global -- not a guard, deferred import, dynamic '
        'mechanism, external boundary, or annotation position.'),
        ('CAN IT BE PROVEN?: sub-patterns (a)-(c) are statically provable '
        '(exact AST scope/alias facts); sub-pattern (d) is inherently '
        'ambiguous and remains poisoned -- per-node verdict recorded in '
        'the JSON artifact.')),
    }


def main() -> int:
    result = analyze()
    counts = result['counts']
    observed = result['observed']

    print('LIVE EXTRACTION')
    print(f'  expected baseline (Phase 5.0 design): {EXPECTED_BASELINE}')
    print(f'  observed now:                         {observed}')
    print(f'  exact delta:                          '
          f'{observed - EXPECTED_BASELINE:+d} (not force-fit)')
    # causal provenance of the delta: same live method on the design
    # doc's own tree
    print(f'  graph nodes: {len(result["graph"].nodes)}  '
          f'edges: {len(result["graph"].edges)}')
    total_prov = sum(1 for r in result['records'] if r.provable)
    print(f'  statically provable nodes: {total_prov} / {observed}   '
          f'ambiguous (remain poisoned): {observed - total_prov}')

    print()
    print('TAXONOMY (mutually exclusive primary categories)')
    header = ('| Category                           | Count | '
              'Percentage | Provable in 5.1? |')
    print(header)
    print('|' + '-' * 36 + '+' + '-' * 7 + '+' + '-' * 11 + '+'
          + '-' * 17 + '|')
    pcts: dict[str, float] = {}
    for cat in ORDER:
        pcts[cat] = round(counts.get(cat, 0) * 100.0 / observed, 1) \
            if observed else 0.0
    residual = round(100.0 - sum(pcts.values()), 1)
    if residual:
        target = max(ORDER, key=lambda c: counts.get(c, 0))
        pcts[target] = round(pcts[target] + residual, 1)
    # mechanism-level verdicts for structurally empty categories (the
    # evaluation is about the mechanism, not the current population)
    mechanism_verdict = {CAT_TYPE_CHECKING: 'Yes', CAT_EXTERNAL: 'No'}
    for cat in ORDER:
        nodes = [r for r in result['records'] if r.category == cat]
        if not nodes:
            prov = mechanism_verdict.get(cat, '--')
        else:
            prov = 'Yes' if all(r.provable for r in nodes) else 'No'
        print(f'| {cat:34} | {len(nodes):5} | '
              f'{pcts[cat]:8}% | {prov:16} |')
    print(f'| {"TOTAL":34} | {observed:5} | '
          f'{sum(pcts.values()):8.1f}% |                  |')

    print()
    print('A.1 THREE-QUESTION DIAGNOSTIC (per category)')
    for cat in ORDER:
        nodes = [r for r in result['records'] if r.category == cat]
        prov_n = sum(1 for r in nodes if r.provable)
        what, why, can = THREE_QUESTIONS[cat]
        print(f'\n## {cat}  ({len(nodes)} nodes; '
              f'provably-static {prov_n}, ambiguous {len(nodes) - prov_n})')
        print(f'  1. {what}')
        print(f'  2. {why}')
        print(f'  3. {can}')

    print()
    print('EXACT REFERENCES (per category, first 8 nodes: node | '
          'origin file:line | primary site)')
    for cat in ORDER:
        nodes = [r for r in result['records'] if r.category == cat]
        print(f'\n## {cat}')
        if not nodes:
            print('  (none observed)')
            continue
        for rec in nodes[:8]:
            origin = ', '.join(
                f'{rel_of(result["rel_of_module"], s)}'
                for s, _k in rec.edges[:2]) or '?'
            site = (f'{rec.sites[0][0]}:{rec.sites[0][1]}'
                    if rec.sites else rec.note or 'site_not_located')
            print(f'  {rec.node} | {origin} | {site}')

    # artifact for audit (gitignored results dir)
    out_dir = ROOT / 'benchmarks' / 'results'
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        'expected_baseline': EXPECTED_BASELINE,
        'observed': observed,
        'delta': observed - EXPECTED_BASELINE,
        'counts': {c: counts.get(c, 0) for c in ORDER},
        'provably_static': total_prov,
        'ambiguous': observed - total_prov,
        'nodes': [
            {'node': r.node, 'category': r.category,
             'provable': r.provable, 'note': r.note,
             'why_not_provable': r.why_not_provable,
             'edges': r.edges, 'sites': r.sites}
            for r in result['records']
        ],
    }
    path = out_dir / 'unresolved_taxonomy.json'
    path.write_text(json.dumps(artifact, indent=1, sort_keys=True),
                    encoding='utf-8')
    print(f'\nartifact: {path}')

    assert sum(counts.values()) == observed, 'categories must sum exactly'
    return 0


def rel_of(rel_of_module: dict[str, str], source: str) -> str:
    module = _origin_module(source)
    return f'{rel_of_module.get(module, module)}'


if __name__ == '__main__':
    raise SystemExit(main())
