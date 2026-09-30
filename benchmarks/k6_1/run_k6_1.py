"""K.6.1 versioned benchmark — re-evaluates K.6 T1-T3 + 3 new tasks.
Original K.6 artifacts untouched (benchmarks/k6/ is frozen).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/amir/src/Asha")

import tiktoken

from asha import context_slicer, graph_cache

REPO = Path("/home/amir/src/Asha")
ENC = tiktoken.get_encoding("cl100k_base")
TOKENIZER_VERSION = getattr(tiktoken, "__version__", "0.14.0")

# Frozen task definitions (K.6 originals + 3 new)
TASKS = {
    "T1": {
        "name": "Signature Refactor: collect_worker_evidence",
        "target_symbol": "collect_worker_evidence",
        "target_module": "asha.governance.worker_execution",
        "required_symbols": {"collect_worker_evidence", "baseline_checks",
                             "run_worker_in_worktree"},
        "grep_baseline": 6968,
    },
    "T2": {
        "name": "Data Model Change: FailureIdentity",
        "target_symbol": "FailureIdentity",
        "target_module": "asha.governance.delta",
        "required_symbols": {"FailureIdentity", "extract_failures",
                             "verdict_for"},
        "grep_baseline": 1413,
    },
    "T3": {
        "name": "Workflow Change: default_execute",
        "target_symbol": "default_execute",
        "target_module": "asha.governance.worker_execution",
        "required_symbols": {"default_execute", "dispatch_runner", "execute"},
        "grep_baseline": 9271,
    },
    "T4": {
        "name": "Leaf data model: FailureIdentity.from_dict (consumer rule)",
        "target_symbol": "FailureIdentity.from_dict",
        "target_module": "asha.governance.delta",
        "required_symbols": {"from_dict", "FailureIdentity", "to_dict"},
        "grep_baseline": None,
    },
    "T5": {
        "name": "Large fn: run_worker_in_worktree (multi-dep)",
        "target_symbol": "run_worker_in_worktree",
        "target_module": "asha.governance.worker_execution",
        "required_symbols": {"run_worker_in_worktree", "baseline_checks",
                             "collect_worker_evidence", "dispatch_runner"},
        "grep_baseline": None,
    },
    "T6": {
        "name": "Workflow both-ways: DAGCoordinator.run",
        "target_symbol": "DAGCoordinator.run",
        "target_module": "asha.governance.dag",
        "required_symbols": {"run", "DAGCoordinator", "_run_one",
                             "run_workers_dag", "_set"},
        "grep_baseline": None,
    },
}


def count_tokens(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def grep_context(symbol: str, required: set[str]) -> dict:
    """A1-style: grep files containing the symbol, ±50-line windows."""
    # qualified names (Class.method) search by the method's bare name
    needle = symbol.split(".")[-1]
    hits = []
    for p in (REPO / "asha").rglob("*.py"):
        try:
            if needle in p.read_text(encoding="utf-8", errors="replace"):
                hits.append(p)
        except OSError:
            continue
    parts = []
    for f in hits:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        widx = [i for i, ln in enumerate(lines) if needle in ln]
        win = set()
        for h in widx:
            win.update(range(max(0, h - 50), min(len(lines), h + 51)))
        if win:
            parts.append(f"# FILE {f.relative_to(REPO)}\n" + "\n".join(
                f"{i+1}:{lines[i]}" for i in sorted(win)))
    ctx = "\n\n".join(parts)
    return {"context": ctx, "tokens": count_tokens(ctx)}


def codegraph_context(symbol: str, module: str, *, reverse: bool,
                      max_nodes: int | None) -> dict:
    loaded = graph_cache.load(REPO, REPO / ".k6graph")
    fact = None
    for idx in loaded.sources.values():
        f = idx.symbol(symbol)
        if f is not None:
            fact = f
            break
    if fact is None:
        return {"context": "", "tokens": 0, "error": "symbol not found",
                "completeness": "UNKNOWN", "reasons": ["not indexed"],
                "unresolved": []}
    sl = context_slicer.slice_context(
        fact.source, target_name=symbol, target_module=module,
        graph=loaded.graph, indices=tuple(loaded.sources.values()),
        reverse=reverse, max_nodes=max_nodes)
    ctx = sl.target_source + "\n" + "\n".join(st.text for st in sl.stubs)
    seen = set()
    for st in sl.stubs:
        for tok in st.text.split():
            seen.add(tok.strip("(),:."))
    return {
        "context": ctx,
        "tokens": count_tokens(ctx),
        "completeness": sl.completeness,
        "reasons": list(sl.completeness_reasons),
        "unresolved": list(sl.closure.unresolved),
        "symbol_count": sl.symbol_count,
    }


def completeness_check(ctx: str, required: set[str]) -> dict:
    # symbol-level presence: word-boundary match (a call `foo(` or a def
    # `def foo(` must count as present for `foo`).
    import re
    provided = set()
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", ctx):
        provided.add(tok)
    missing = sorted(required - provided)
    return {"context_complete": not missing, "missing_symbols": missing}


def run_all() -> dict:
    results = {}
    for tid, task in TASKS.items():
        sym = task["target_symbol"]
        req = task["required_symbols"]
        grep = grep_context(sym, req)
        row = {"task": tid, "name": task["name"], "strategies": {}}
        row["strategies"]["Grep(A1)"] = {
            "tokens": grep["tokens"],
            "reduction_vs_grep": 0.0,
            **completeness_check(grep["context"], req),
        }
        # original CodeGraph behavior (forward full-body) approximated by
        # forward slice before stub fix = we cite K.6 numbers instead
        for label, kw in [("CodeGraph-fwd", {"reverse": False,
                                             "max_nodes": None}),
                          ("CodeGraph-rev", {"reverse": True,
                                             "max_nodes": 50}),
                          ("CodeGraph-both", {"reverse": True,
                                              "max_nodes": None})]:
            if label == "CodeGraph-both":
                # combined: forward slice for deps + reverse for callers
                fwd = codegraph_context(sym, task["target_module"],
                                        reverse=False, max_nodes=None)
                rev = codegraph_context(sym, task["target_module"],
                                        reverse=True, max_nodes=50)
                ctx = fwd["context"] + "\n### reverse callers ###\n" + rev["context"]
                comp = completeness_check(ctx, req)
                row["strategies"][label] = {
                    "tokens": count_tokens(ctx), "completeness": "COMPLETE"
                    if comp["context_complete"] else "INCOMPLETE",
                    **comp, "fwd_unresolved": fwd.get("unresolved", []),
                    "rev_unresolved": rev.get("unresolved", []),
                }
            else:
                cg = codegraph_context(sym, task["target_module"], **kw)
                comp = completeness_check(cg.get("context", ""), req)
                row["strategies"][label] = {
                    "tokens": cg.get("tokens", 0),
                    "completeness": cg.get("completeness", "UNKNOWN"),
                    "reasons": cg.get("reasons", []),
                    **comp,
                }
        # compute reductions vs grep
        g = row["strategies"]["Grep(A1)"]["tokens"]
        for label in ("CodeGraph-fwd", "CodeGraph-rev", "CodeGraph-both"):
            if label in row["strategies"]:
                t = row["strategies"][label]["tokens"]
                row["strategies"][label]["reduction_vs_grep"] = round(
                    (g - t) / g * 100, 1) if g else 0.0
        results[tid] = row
    return results


if __name__ == "__main__":
    out = run_all()
    res = Path("/home/amir/src/Asha/benchmarks/k6_1/results")
    res.mkdir(parents=True, exist_ok=True)
    (res / "results.json").write_text(json.dumps(out, indent=2),
                                      encoding="utf-8")
    print("wrote benchmarks/k6_1/results/results.json")