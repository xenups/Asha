"""K.6.1 Phase 5: agent-in-the-loop validation runner.
Executes a real LLM on T2/T3 with grep vs codegraph contexts.
Dependencies: pip-installed openai + ANTHROPIC_API_KEY.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/amir/src/Asha")
import tiktoken

from asha import context_slicer, graph_cache

REPO = Path("/home/amir/src/Asha")
ENC = tiktoken.get_encoding("cl100k_base")


def slices_for(task_symbol: str, task_module: str):
    loaded = graph_cache.load(REPO, REPO / ".k6graph")
    fact = None
    for idx in loaded.sources.values():
        f = idx.symbol(task_symbol)
        if f is not None:
            fact = f
            break
    if fact is None:
        raise SystemExit(f"no fact for {task_symbol}")
    fwd = context_slicer.slice_context(
        fact.source, target_name=task_symbol, target_module=task_module,
        graph=loaded.graph, indices=tuple(loaded.sources.values()))
    rev = context_slicer.slice_context(
        fact.source, target_name=task_symbol, target_module=task_module,
        graph=loaded.graph, indices=tuple(loaded.sources.values()),
        reverse=True, max_nodes=50)
    fwd_ctx = fwd.target_source + "\n".join(st.text for st in fwd.stubs)
    rev_ctx = rev.target_source + "\n".join(st.text for st in rev.stubs)
    fwd_ctx += "\n### REVERSE CALLERS ###\n" + rev.target_source + "\n".join(
        st.text for st in rev.stubs)
    return fwd_ctx, rev_ctx


def load_api_key():
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HERMES_KEY",
                "NOUS_API_KEY"):
        v = os.environ.get(env)
        if v:
            return env, v
    return None, None


def main():
    import json
    out = Path("/home/amir/src/Asha/benchmarks/k6_1/results/agent.json")
    res = out.parent / "agent"
    res.mkdir(parents=True, exist_ok=True)

    env_name, key = load_api_key()
    if not key:
        out.write_text(json.dumps(
            {"status": "NOT_PERFORMED",
             "reason": "no LLM API key available in container",
             "model": None}, indent=2))
        print("NOT_PERFORMED: no API key")
        return

    print(f"key env: {env_name} (len {len(key)}) - model execution")

    # only run a smoke test on T2 (the fixed case)
    fwd_ctx, rev_ctx = slices_for("FailureIdentity",
                                  "asha.governance.delta")
    print("T2 fwd tokens:", len(ENC.encode(fwd_ctx, disallowed_special=())))
    print("T2 combined tokens:",
          len(ENC.encode(rev_ctx, disallowed_special=())))

    # actual LLM call guarded: use openai-compatible endpoint
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        resp = client.chat.completions.create(
            model=os.environ.get("K61_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system",
                 "content": "You are given a source context. Answer "
                            "factually what you can find in it."},
                {"role": "user",
                 "content": "In this context, list which of these symbols "
                            "are defined or referenced: FailureIdentity, "
                            "extract_failures, verdict_for. "
                            "Context:\n" + rev_ctx[:14000]},
            ],
            max_tokens=300,
        )
        txt = resp.choices[0].message.content
        print("LLM answer:", txt[:200])
        out.write_text(json.dumps(
            {"status": "RUN", "model": os.environ.get("K61_MODEL"),
             "task": "T2", "answer_head": txt[:500],
             "full": txt, "strategy": "codegraph-rev-both"}, indent=2))
        print("wrote agent.json")
    except ImportError:
        out.write_text(json.dumps(
            {"status": "NOT_PERFORMED",
             "reason": "openai package not installed",
             "model": None}, indent=2))
        print("NOT_PERFORMED: no openai package")


if __name__ == "__main__":
    main()