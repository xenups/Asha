"""Phase 5.1 -- real token economics harness (spec 2.C).

NO MOCK METRICS: if a real tokenizer (tiktoken with cl100k_base /
o200k_base) cannot provide an encoding, the token metric status is
reported as UNAVAILABLE and only raw source-BYTE reduction is output.
Estimated/synthetic token counts are never substituted.

Workloads (representative, from the live CodeGraph of this repository):
- leaf bug-fix      -> largest function of asha/metrics.py
- mid-level refactor-> asha/codegraph.py:build_graph
- interface change  -> asha/ast_indexer.py:index_module

Raw Context    = every indexed repository source concatenated (the
                 unfiltered context an unscoped workflow would carry).
Surgical Context = context_slicer output: target source + dependency
                 stubs (the proven closure, stubbed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from asha import codegraph, context_slicer, scoping
from asha.ast_indexer import ModuleIndex

if TYPE_CHECKING:
    import tiktoken

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / 'benchmarks' / 'results'


def _detect_tokenizer() -> tuple[bool, str, tiktoken.Encoding | None]:
    """REAL tokenizer only. Any failure -> UNAVAILABLE (no substitutes)."""
    try:
        import tiktoken
    except Exception as exc:
        return False, (f'UNAVAILABLE (tiktoken not importable: ' \
                      f'{type(exc).__name__}: {exc})'), None
    last = 'no encoding attempted'
    for encoding in ('cl100k_base', 'o200k_base'):
        try:
            return True, f'AVAILABLE ({encoding})', \
                tiktoken.get_encoding(encoding)
        except Exception as exc:
            last = f'{type(exc).__name__}: {exc}'
            continue
    return False, ('UNAVAILABLE (tiktoken installed but neither ' \
                  f'cl100k_base nor o200k_base encoding loadable: ' \
                  f'{last})'), None


def _primary_function(index: ModuleIndex) -> str | None:
    """Deterministic workload pick: biggest function source in module."""
    best = ''
    best_name = None
    for fact in index.symbols:
        if fact.kind != 'function':
            continue
        if len(fact.source) > len(best):
            best, best_name = fact.source, fact.name
    return best_name


def main() -> int:
    tokenizer_ok, tokenizer_status, encoder = _detect_tokenizer()
    sources, texts, module_of_rel, _rel_of_module = scoping._index_repository(
        ROOT)
    graph = codegraph.build_graph(tuple(sources.values()))
    raw_text = '\n'.join(texts[m] for m in sorted(texts))
    raw_bytes = len(raw_text.encode('utf-8'))

    named = [
        ('leaf bug-fix', 'asha/metrics.py',
         _primary_function(sources['asha.metrics'])),
        ('mid-level refactor', 'asha/codegraph.py', 'build_graph'),
        ('interface change', 'asha/ast_indexer.py', 'index_module'),
    ]

    rows: list[dict[str, object]] = []
    for label, rel, symbol in named:
        module = module_of_rel[rel]
        if not symbol:
            rows.append({'workload': label, 'error': 'no primary symbol'})
            continue
        slice_ = context_slicer.slice_context(
            texts[module], target_name=symbol, target_module=module,
            graph=graph, indices=tuple(sources.values()))
        surgical_text = slice_.target_source + '\n' + '\n'.join(
            stub.text for stub in slice_.stubs)
        surgical_bytes = len(surgical_text.encode('utf-8'))
        row: dict[str, object] = {
            'workload': label,
            'target': f'{module}:{symbol}',
            'raw_bytes': raw_bytes,
            'surgical_bytes': surgical_bytes,
            'byte_reduction_pct': round(
                (1.0 - surgical_bytes / raw_bytes) * 100.0, 1),
        }
        if tokenizer_ok and encoder is not None:
            raw_tokens = len(encoder.encode_ordinary(raw_text))
            surgical_tokens = len(
                encoder.encode_ordinary(surgical_text))
            row.update({
                'raw_tokens': raw_tokens,
                'surgical_tokens': surgical_tokens,
                'tokens_saved': raw_tokens - surgical_tokens,
                'token_reduction_pct': round(
                    (1.0 - surgical_tokens / raw_tokens) * 100.0, 1),
            })
        rows.append(row)

    payload: dict[str, object] = {
        'tokenizer_status': tokenizer_status,
        'raw_context_bytes': raw_bytes,
        'workloads': rows,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / 'token_economics.json').write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')

    print('TOKEN ECONOMICS (real measurements, no estimates)')
    print(f'  tokenizer status: {tokenizer_status}')
    print(f'  raw context (whole repository): {raw_bytes} bytes'
          + ('' if not tokenizer_ok else
             f' = {len(encoder.encode_ordinary(raw_text))} tokens'
             if encoder is not None else ''))
    for row in rows:
        if 'error' in row:
            print(f"  {row['workload']}: ERROR {row['error']}")
            continue
        line = (f"  {row['workload']} ({row['target']}): "
                f"surgical {row['surgical_bytes']} bytes "
                f"({row['byte_reduction_pct']}% reduction)")
        if 'raw_tokens' in row:
            line += (f" | tokens raw={row['raw_tokens']} "
                     f"surgical={row['surgical_tokens']} "
                     f"saved={row['tokens_saved']} "
                     f"({row['token_reduction_pct']}% reduction)")
        print(line)
    if not tokenizer_ok:
        print('  token metrics: UNAVAILABLE -- byte reduction only '
              '(no synthetic counts produced)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
