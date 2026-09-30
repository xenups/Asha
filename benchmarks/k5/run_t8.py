"""K.5.2 T8 probe — AST/CodeGraph vs textual grep for affected-test discovery.

Independent research probe (NOT part of the A/B/C governance aggregate).
Measures precision/recall/latency of two test-targeting strategies on a
real signature-change patch.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

REPO = Path("/home/amir/src/Asha")
OUT = Path("/home/amir/src/Asha/benchmarks/k5/results/t8-probe.json")

SIGNATURE = "def render_manifest("
TEST_ANCHOR = "render_manifest"


def build_probe_repo() -> Path:
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="k5-t8-"))
    files = {
        "app.py": (
            "from typing import Any\n\n\n"
            "def render_manifest(data: Any) -> dict:\n"
            "    return {'items': data}\n\n\n"
            "def render_manifest_v2(data: Any) -> dict:\n"
            "    return {'items': data, 'v2': True}\n"
        ),
        "tests/test_render.py": (
            "from app import render_manifest\n\n\n"
            "def test_render():\n"
            "    assert render_manifest([1]) == {'items': [1]}\n\n\n"
            "def test_render_v2():\n"
            "    assert render_manifest_v2([1])['v2'] is True\n"
        ),
        "tests/test_other.py": (
            "from app import render_manifest as rm\n\n\n"
            "def test_rm_alias():\n"
            "    assert rm([]) == {'items': []}\n"
        ),
        "tests/test_unrelated.py": (
            "def test_unrelated():\n"
            "    assert 1 + 1 == 2\n"
        ),
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t8@t8"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t8"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"],
                   check=True)
    return root


TRUE_TARGETS = {"tests/test_render.py", "tests/test_other.py"}


def grep_search(root: Path) -> tuple[set[str], float]:
    t0 = time.perf_counter()
    hits = set()
    for t in root.rglob("tests/*.py"):
        txt = t.read_text(encoding="utf-8", errors="replace")
        if TEST_ANCHOR in txt:
            hits.add(str(t.relative_to(root)))
    return hits, (time.perf_counter() - t0) * 1000


def ast_search(root: Path) -> tuple[set[str], float]:
    """AST call-graph: find tests importing/aliasing the target symbol."""
    import ast as _ast

    t0 = time.perf_counter()
    target = "render_manifest"
    # module -> imported names that alias the target
    tests: dict[str, set[str]] = {}
    for t in root.rglob("tests/*.py"):
        rel = str(t.relative_to(root))
        try:
            tree = _ast.parse(t.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        names: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                for al in node.names:
                    if al.name == target:
                        names.add(al.asname or al.name)
            elif isinstance(node, _ast.Import):
                for al in node.names:
                    if al.name == target or (al.asname and target in al.name):
                        names.add(al.asname or al.name)
        tests[rel] = names
    # Any test that references one of the imported alias names anywhere
    # in its body is affected.
    affected = set()
    for rel, names in tests.items():
        if not names:
            continue
        body = (root / rel).read_text(encoding="utf-8", errors="replace")
        for n in names:
            if n in body:
                affected.add(rel)
                break
    return affected, (time.perf_counter() - t0) * 1000


def main() -> None:
    root = build_probe_repo()
    print("probe repo:", root)
    results = {}
    for name, fn in (("grep", grep_search), ("ast", ast_search)):
        hits, ms = fn(root)
        tp = hits & TRUE_TARGETS
        fp = hits - TRUE_TARGETS
        fn_set = TRUE_TARGETS - hits
        precision = len(tp) / len(hits) if hits else 0.0
        recall = len(tp) / len(TRUE_TARGETS) if TRUE_TARGETS else 0.0
        results[name] = {
            "true_targets": sorted(TRUE_TARGETS),
            "hits": sorted(hits),
            "true_positive": sorted(tp),
            "false_positive": sorted(fp),
            "missed_targets": sorted(fn_set),
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "search_time_ms": round(ms, 1),
            "graph_build_time_ms": 0.0,
            "memory_usage_bytes": 0,
        }
        print(f"{name}: precision={precision:.2f} recall={recall:.2f} "
              f"hits={sorted(hits)} missed={sorted(fn_set)} {ms:.1f}ms")
    payload = {
        "benchmark_run_id": "k5-T8-single-shot",
        "task_id": "T8",
        "path_id": "T8-Grep/T8-AST",
        "probe_repo": str(root),
        "patch": SIGNATURE + " (signature rename → alias import)",
        "results": results,
        "note": "T8 excluded from A/B/C aggregate per spec Phase 6.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)
    print("wrote", OUT)


if __name__ == "__main__":
    main()