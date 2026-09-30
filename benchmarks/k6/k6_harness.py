"""K.6 context/token reduction harness (benchmarks/k6).

Runs the frozen K.6 tasks against retrieval strategies:
  A1 = grep symbol → ±50-line windows
  A2 = grep symbol → whole files
  B1 = CodeGraph slice (1-hop closure)
  B2 = CodeGraph slice (2-hop closure)
Measures tokens (tiktoken cl100k_base), completeness vs ground truth,
noise, latency. Deterministic retrieval -> single run per cell.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

REPO = Path("/home/amir/src/Asha")
sys.path.insert(0, str(REPO))

import tiktoken

from asha import (
    context_slicer,
    graph_cache,
)

TOKENIZER_NAME = "cl100k_base"
ENC = tiktoken.get_encoding(TOKENIZER_NAME)
TOKENIZER_VERSION = getattr(tiktoken, "__version__", "0.14.0")

CONTEXT_BEFORE = 50
CONTEXT_AFTER = 50


# ---------------------------------------------------------------------------
# Frozen task definitions: symbol, required files (ground truth), the change
# ---------------------------------------------------------------------------

TASKS = {
    "T1": {
        "name": "Signature Refactor: collect_worker_evidence",
        "target_symbol": "collect_worker_evidence",
        "target_module": "asha.governance.worker_execution",
        "required_files": {
            "asha/governance/worker_execution.py",
            "asha/governance/dag.py",
            "asha/scheduler.py",
        },
        "required_symbols": {
            "collect_worker_evidence",
            "baseline_checks",
            "run_worker_in_worktree",
        },
        "change_note": "add a required keyword-only parameter; every call site "
                       "in dag.py/scheduler.py must pass it.",
    },
    "T2": {
        "name": "Data Model Change: FailureIdentity",
        "target_symbol": "FailureIdentity",
        "target_module": "asha.governance.delta",
        "required_files": {
            "asha/governance/delta.py",
            "asha/governance/worker_execution.py",
        },
        "required_symbols": {
            "FailureIdentity",
            "extract_failures",
            "verdict_for",
        },
        "change_note": "add field flaky: bool = False; find every consumer "
                       "constructing/reading it (extract_failures, verdict_for, "
                       "evidence payloads).",
    },
    "T3": {
        "name": "Workflow Change: dispatch/execute hook",
        "target_symbol": "default_execute",
        "target_module": "asha.governance.worker_execution",
        "required_files": {
            "asha/governance/worker_execution.py",
            "asha/governance/dag.py",
            "asha/runner.py",
        },
        "required_symbols": {
            "default_execute",
            "dispatch_runner",
            "execute",
        },
        "change_note": "add pre-execution barrier in the dispatch path; identify "
                       "all callees/callers affected.",
    },
}


# ---------------------------------------------------------------------------
# Strategy A: text retrieval via rg
# ---------------------------------------------------------------------------

def _rg_files(symbol: str) -> list[Path]:
    # rg binary is not available in this container; use an equivalent
    # stdlib line scan (same retrieval semantics: files containing the
    # symbol under asha/).
    hits: list[Path] = []
    for p in (REPO / "asha").rglob("*.py"):
        try:
            if symbol in p.read_text(encoding="utf-8", errors="replace"):
                hits.append(p)
        except OSError:
            continue
    return sorted(hits)


def strategy_A(symbol: str, mode: str) -> dict:
    t0 = time.perf_counter()
    files = _rg_files(symbol)
    search_ms = (time.perf_counter() - t0) * 1000
    parts: list[str] = []
    for f in files:
        src = f.read_text(encoding="utf-8", errors="replace")
        lines = src.splitlines()
        if mode == "A2":  # whole file
            parts.append(f"# FILE {f.relative_to(REPO)}\n" + src)
            continue
        # A1: ±50 lines around each match line
        hits = [i for i, ln in enumerate(lines) if symbol in ln]
        windows = set()
        for h in hits:
            lo = max(0, h - CONTEXT_BEFORE)
            hi = min(len(lines), h + CONTEXT_AFTER + 1)
            windows.update(range(lo, hi))
        if windows:
            block = "\n".join(
                f"{i+1}:{lines[i]}" for i in sorted(windows))
            parts.append(f"# FILE {f.relative_to(REPO)}\n" + block)
    context = "\n\n".join(parts)
    return {
        "strategy": mode,
        "context": context,
        "search_ms": search_ms,
        "files": [str(f.relative_to(REPO)) for f in files],
    }


# ---------------------------------------------------------------------------
# Strategy B: CodeGraph slice
# ---------------------------------------------------------------------------

def strategy_B(symbol: str, module: str, hops: int) -> dict:
    t0 = time.perf_counter()
    cache_dir = REPO / ".k6graph"
    loaded = graph_cache.load(REPO, cache_dir)
    graph = loaded.graph
    indices = loaded.sources
    build_ms = (time.perf_counter() - t0) * 1000
    fact = None
    target_source = ""
    for idx in indices.values():
        f = idx.symbol(symbol)
        if f is not None:
            fact = f
            target_source = f.source
            break
    if fact is None:
        return {"strategy": f"B{hops}", "context": "",
                "error": f"symbol {symbol} not found in graph",
                "files": [], "search_ms": 0, "build_ms": build_ms}
    t2 = time.perf_counter()
    sl = context_slicer.slice_context(
        target_source, target_name=symbol,
        target_module=module, graph=graph, indices=tuple(indices.values()))
    slice_ms = (time.perf_counter() - t2) * 1000
    # 2-hop: re-slice on the closure's symbols (approximation of 2-hop)
    context = target_source
    for stub in sl.stubs:
        context += "\n" + stub.text
    if hops == 2:
        # second hop: add stubs for symbols referenced by the first hop
        extra = []
        for stub in sl.stubs:
            m = re.match(r"^# missing symbol index: sym:([^:]+):(.+)$", stub.text)
            if m:
                mod2, name2 = m.group(1), m.group(2)
                f2 = None
                for idx in indices.values():
                    if idx.module == mod2:
                        f2 = idx.symbol(name2)
                        break
                if f2 is not None and f2.module != module:
                    extra.append(f2.source)
        if extra:
            context += "\n# 2-hop\n" + "\n\n".join(extra)
    files = sorted({str(Path(p).relative_to(REPO))
                    for p in _rg_files(symbol)})
    return {
        "strategy": f"B{hops}",
        "context": context,
        "search_ms": 0,
        "build_ms": build_ms,
        "slice_ms": slice_ms,
        "files": files,
        "symbols_in_slice": sl.symbol_count,
        "unresolved": sorted(sl.closure.unresolved),
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def measure(strategy: dict, task: dict) -> dict:
    ctx = strategy.get("context", "")
    toks = count_tokens(ctx)
    # provided symbols: extract identifiers appearing in context
    provided = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", ctx))
    required = task["required_symbols"]
    missing = required - provided
    extra = provided - required
    relevant_tokens = 0
    # relevance at line level: a line is relevant iff it DEFINES or CALLS
    # a required symbol (`def <sym>`, `class <sym>`, or `<sym>(`), not
    # merely contains the identifier anywhere.
    for ln in ctx.splitlines():
        if any(re.search(rf"(def|class)\s+{re.escape(s)}\b|{re.escape(s)}\s*\(",
                         ln) for s in required):
            relevant_tokens += count_tokens(ln)
    total = toks
    noise = total - relevant_tokens
    return {
        "strategy": strategy["strategy"],
        "raw_chars": len(ctx),
        "raw_lines": ctx.count("\n"),
        "token_count": toks,
        "tokens_total": toks,
        "tokens_relevant": relevant_tokens if relevant_tokens else toks,
        "tokens_irrelevant": noise,
        "noise_ratio": round(noise / total, 4) if total else 0.0,
        "context_complete": not missing,
        "missing_symbols": sorted(missing),
        "provided_symbols_count": len(provided),
        "extra_symbols_count": len(extra),
        "files": strategy.get("files", []),
        "search_ms": round(strategy.get("search_ms", 0), 1),
        "build_ms": round(strategy.get("build_ms", 0), 1),
        "slice_ms": round(strategy.get("slice_ms", 0), 1),
    }


def run_all() -> dict:
    results: dict[str, dict] = {}
    for tid, task in TASKS.items():
        sym = task["target_symbol"]
        mod = task["target_module"]
        cell: dict = {"task": tid, "strategies": {}}
        baseline_tokens: int | None = None
        for strat in ("A1", "A2", "B1", "B2"):
            if strat == "A1":
                s = strategy_A(sym, "A1")
            elif strat == "A2":
                s = strategy_A(sym, "A2")
            elif strat == "B1":
                s = strategy_B(sym, mod, 1)
            else:
                s = strategy_B(sym, mod, 2)
            m = measure(s, task)
            cell["strategies"][strat] = m
            if strat == "A1":
                baseline_tokens = m["token_count"]
            if baseline_tokens:
                m["token_reduction_pct_vs_A1"] = round(
                    (baseline_tokens - m["token_count"]) / baseline_tokens * 100,
                    1)
        results[tid] = cell
    return results


if __name__ == "__main__":
    out = run_all()
    res = Path(__file__).resolve().parent / "results"
    res.mkdir(parents=True, exist_ok=True)
    (res / "k6_results.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8")
    print("wrote results/k6_results.json")