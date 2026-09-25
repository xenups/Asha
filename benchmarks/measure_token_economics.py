"""Phase 5.1 -- real token economics harness (spec 2.C), OFFLINE.

NO MOCK METRICS: if a real tokenizer (tiktoken cl100k_base) cannot be
loaded, the token metric status is reported as UNAVAILABLE and only raw
source-BYTE reduction is output. Estimated/synthetic token counts are
never substituted.

OFFLINE CONTRACT (spec 3):
- the BPE asset is the committed local file
  ``benchmarks/data/cl100k_base.tiktoken``;
- authenticity: asset sha256 must equal tiktoken's own
  ``expected_hash`` constant from ``tiktoken_ext.openai_public``;
- loading uses tiktoken's supported ``TIKTOKEN_CACHE_DIR`` mechanism
  (URL sha1 cache key) populated from that local file;
- sockets are HARD-DISABLED during encoding load + first encode, so a
  successful load is itself the network-independence proof.

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

import hashlib
import json
import os
import re
import socket
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from asha import codegraph, context_slicer, scoping
from asha.ast_indexer import ModuleIndex

if TYPE_CHECKING:
    import tiktoken

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / 'benchmarks' / 'results'
LOCAL_ASSET = ROOT / 'benchmarks' / 'data' / 'cl100k_base.tiktoken'
CL100K_URL = ('https://openaipublic.blob.core.windows.net/encodings/'
              'cl100k_base.tiktoken')


def _detect_tokenizer() -> tuple[bool, str, tiktoken.Encoding | None,
                                 dict]:
    """Genuine OFFLINE BPE only (spec 3): load cl100k_base from the
    committed local asset via tiktoken's supported TIKTOKEN_CACHE_DIR
    mechanism with sockets HARD-DISABLED during load and first encode.

    Authenticity: asset sha256 must equal tiktoken's own expected_hash
    constant. No estimates, no approximations, no fallback formulas:
    any failure -> UNAVAILABLE (byte reduction only).
    """
    proof: dict[str, object] = {
        'local_asset': str(LOCAL_ASSET.relative_to(ROOT)),
        'expected_hash_match': False,
        'network_blocked_during_load': False,
        'offline_roundtrip': False,
        'known_ids_hello_world': None,
    }
    if not LOCAL_ASSET.is_file():
        return (False,
                ('UNAVAILABLE (local asset benchmarks/data/'
                 'cl100k_base.tiktoken missing)'), None, proof)

    data = LOCAL_ASSET.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    try:
        from tiktoken_ext import openai_public  # type: ignore[import-untyped]
        source = Path(openai_public.__file__).read_text(encoding='utf-8')
        block = source.split('def cl100k_base():', 1)[1]
        match = re.search(r'expected_hash="([0-9a-f]{64})"', block)
        if match is None:
            raise ValueError('expected_hash constant not found in source')
        proof['expected_hash_match'] = actual == match.group(1)
    except Exception as exc:
        return False, (f'UNAVAILABLE (expected-hash provenance check '
                       f'failed: {type(exc).__name__}: {exc})'), None, proof
    if not proof['expected_hash_match']:
        return False, (f'UNAVAILABLE (asset sha256 {actual[:16]}... != '
                       f'tiktoken expected_hash)'), None, proof

    # supported local loading: tiktoken cache key = sha1 of the URL
    cache_dir = Path(tempfile.gettempdir()) / 'asha-tiktoken-offline'
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / hashlib.sha1(CL100K_URL.encode()).hexdigest()
    if not cache_file.is_file() or cache_file.read_bytes() != data:
        cache_file.write_bytes(data)
    os.environ['TIKTOKEN_CACHE_DIR'] = str(cache_dir)

    real_socket = socket.socket

    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise OSError('network disabled (offline tokenizer verification)')

    encoder: tiktoken.Encoding | None = None
    socket.socket = _blocked  # type: ignore[assignment, misc]  # hard block
    try:
        import tiktoken
        encoder = tiktoken.get_encoding('cl100k_base')
        probe = encoder.encode('hello world')
        proof['known_ids_hello_world'] = list(probe)
        proof['offline_roundtrip'] = encoder.decode(probe) == 'hello world'
    except Exception as exc:
        return False, (f'UNAVAILABLE (offline load failed under network '
                       f'block: {type(exc).__name__}: {exc})'), None, proof
    finally:
        socket.socket = real_socket  # type: ignore[misc]
    proof['network_blocked_during_load'] = True
    if not proof['offline_roundtrip']:
        return (False, 'UNAVAILABLE (encode/decode roundtrip failed)',
                None, proof)
    return (True, 'AVAILABLE (cl100k_base, offline, hash-verified)',
            encoder, proof)


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
    (tokenizer_ok, tokenizer_status, encoder,
     tokenizer_proof) = _detect_tokenizer()
    sources, texts, module_of_rel, _rel_of_module = scoping._index_repository(
        ROOT)
    graph = codegraph.build_graph(tuple(sources.values()))
    raw_text = '\n'.join(texts[m] for m in sorted(texts))
    raw_bytes = len(raw_text.encode('utf-8'))
    raw_tokens = (len(encoder.encode_ordinary(raw_text))
                  if tokenizer_ok and encoder is not None else None)

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
        if (tokenizer_ok and encoder is not None
                and raw_tokens is not None):
            surgical_tokens = len(encoder.encode_ordinary(surgical_text))
            row.update({
                'raw_tokens': raw_tokens,
                'surgical_tokens': surgical_tokens,
                'tokens_saved': raw_tokens - surgical_tokens,
                'token_reduction_pct': round(
                    (1.0 - surgical_tokens / raw_tokens) * 100.0, 1),
            })
        rows.append(row)

    token_rows = [r for r in rows if 'token_reduction_pct' in r]
    token_values = [float(r['token_reduction_pct'])  # type: ignore[arg-type]
                    for r in token_rows]
    avg_reduction = (round(sum(token_values) / len(token_values), 1)
                     if token_values else None)

    payload: dict[str, object] = {
        'tokenizer_status': tokenizer_status,
        'tokenizer': 'cl100k_base' if tokenizer_ok else None,
        'network_independent': bool(
            tokenizer_proof['network_blocked_during_load']),
        'tokenizer_proof': tokenizer_proof,
        'raw_context_bytes': raw_bytes,
        'raw_context_tokens': raw_tokens,
        'average_token_reduction_pct': avg_reduction,
        'workloads': rows,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / 'token_economics.json').write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')

    print('TOKEN ECONOMICS (real measurements, no estimates)')
    print(f'  tokenizer status: {tokenizer_status}')
    print(f'  local asset: {tokenizer_proof["local_asset"]}')
    print(f'  expected_hash match: '
          f'{tokenizer_proof["expected_hash_match"]}')
    print(f'  network-blocked during load: '
          f'{tokenizer_proof["network_blocked_during_load"]}')
    print(f'  raw context (whole repository): {raw_bytes} bytes'
          + (f' = {raw_tokens} tokens' if raw_tokens is not None else ''))
    for row in rows:
        if 'error' in row:
            print(f"  {row['workload']}: ERROR {row['error']}")
            continue
        line = (f"  {row['workload']} ({row['target']}): "
                f"surgical {row['surgical_bytes']} bytes "
                f"({row['byte_reduction_pct']}% byte reduction)")
        if 'raw_tokens' in row:
            line += (f" | tokens raw={row['raw_tokens']} "
                     f"surgical={row['surgical_tokens']} "
                     f"saved={row['tokens_saved']} "
                     f"({row['token_reduction_pct']}% reduction)")
        print(line)
    if avg_reduction is not None:
        print(f'  average context reduction: {avg_reduction}% (tokens)')
    if not tokenizer_ok:
        print('  token metrics: UNAVAILABLE -- byte reduction only '
              '(no synthetic counts produced)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
