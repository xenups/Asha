"""H-AST: empirical AST/CodeGraph vs text-first benchmark (read-only).

Runs 5 deterministic engineering tasks against planted fixtures with
independently known ground truth. Compares:

  Mode A (TEXT-FIRST): grep-based textual search over fixture files.
  Mode B (STRUCTURAL): ast_indexer + codegraph + context_slicer.

Everything runs in an isolated temp dir. The canonical Asha repo is
NEVER written to (no index artifacts, no cache, no edits).

Ground truth is planted at fixture construction and NEVER derived from
the AST machinery under test.

Deterministic: same fixture + same task text + same inputs -> same
result, so a single run per Mode×Task is the demonstrated variance
(no randomness in either mode's search).
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- fixtures

T1_CALLER = """\
from pkg.utils import normalize_name

def handle(event):
    # dispatch: route by normalized name
    return normalize_name(event["name"]).upper()
"""

T1_CALLER2 = """\
from pkg.utils import normalize_name

def render(item):
    return f"{{prefix}}:{{normalize_name(item)}}"
"""

T1_UTILS = """\
def normalize_name(name: str) -> str:
    \"\"\"Old signature: takes str, returns str.\"\"\"
    return name.strip().lower()
"""

T1_SUB = """\
from pkg.utils import normalize_name

def inner(x):
    # used by caller2 through re-export
    return normalize_name(x)
"""

T1_ROUTE = """\
from pkg import sub
from pkg.utils import normalize_name

def route(payload):
    if payload.get("kind") == "sub":
        return sub.inner(payload["value"])
    return normalize_name(payload["value"])
"""

T2_BASE = """\
class Context:
    def __init__(self):
        self.task_id = ""
        self.tenant = "default"
        self.checks = []
"""

T2_MID = """\
from pkg.base import Context

def forward(ctx: Context) -> Context:
    return ctx
"""

T2_LEAF = """\
from pkg.mid import forward

def consume(raw):
    ctx = forward(raw)
    return ctx.checks
"""

T2_SER = """\
from pkg.base import Context
from pkg.mid import forward

def serialize(ctx: Context) -> str:
    return f"{{ctx.task_id}}|{{ctx.tenant}}"
"""

T3_A = """\
def load_config(env):
    return {"db": env.get("DB_URL"), "retries": env.get("RETRIES", 3)}
"""

T3_B = """\
from pkg.config import load_config

def build_pool(env):
    cfg = load_config(env)
    return {"host": cfg["db"], "retries": cfg["retries"]}
"""

T3_C = """\
from pkg.pool import build_pool

def open_connection(env):
    pool = build_pool(env)
    return pool["host"].split(":")[0]
"""

T3_SEED = """\
from pkg.conn import open_connection

def main():
    return open_connection({})
"""

T4_ENUM = """\
class Mode:
    FAST = "fast"
    FULL = "full"
    DRY = "dry"
"""

T4_A = """\
from pkg.enums import Mode

def choose(x):
    return Mode.FAST if x else Mode.FULL
"""

T4_B = """\
from pkg.enums import Mode

def describe(m):
    return {"mode": m.value}
"""

T4_C = """\
from pkg.enums import Mode

def validate(m):
    if m not in (Mode.FAST, Mode.FULL):
        raise ValueError(m)
    return m
"""

T4_SER = """\
from pkg.enums import Mode

def to_wire(m):
    return m.name.lower()
"""

T5_DEAD = """\
# genuinely unused: no callers anywhere
def obsolete_helper(x):
    return x * 2
"""

T5_LIVE = """\

from pkg.registry import REGISTRY

def live_fn(x):
    return x + 1

# statically referenced by register()
def register(name, fn):
    REGISTRY[name] = fn
"""

T5_DYNAMIC = """\
import importlib

REGISTRY = {}

def register_dynamic(name):
    # dynamic reference: resolves at runtime via importlib
    mod = importlib.import_module("pkg.dyn_target")
    REGISTRY[name] = mod.handler
"""

T5_DYN_TARGET = """\
def handler(x):
    return x - 1
"""

T5_MAIN = """\
from pkg.live_module import register, live_fn
from pkg.registry import REGISTRY, register_dynamic

register("live", live_fn)
register_dynamic("dyn")
print(len(REGISTRY))
"""

T5_STATIC_REF = """\
# statically referenced but never called
from pkg.live_module import live_fn

def unused_wrapper():
    return live_fn
"""


FIXTURES: dict[str, dict[str, str]] = {
    "t1_blast": {
        "__init__.py": "",
        "caller.py": T1_CALLER,
        "caller2.py": T1_CALLER2,
        "utils.py": T1_UTILS,
        "route.py": T1_ROUTE,
        "sub/__init__.py": "",
        "sub/inner.py": T1_SUB,
    },
    "t2_api": {
        "__init__.py": "",
        "base.py": T2_BASE,
        "mid.py": T2_MID,
        "leaf.py": T2_LEAF,
        "ser.py": T2_SER,
    },
    "t3_trace": {
        "__init__.py": "",
        "config.py": T3_A,
        "pool.py": T3_B,
        "conn.py": T3_C,
        "seed.py": T3_SEED,
    },
    "t4_enum": {
        "__init__.py": "",
        "enums.py": T4_ENUM,
        "use_a.py": T4_A,
        "use_b.py": T4_B,
        "use_c.py": T4_C,
        "ser.py": T4_SER,
    },
    "t5_dead": {
        "__init__.py": "",
        "dead_module.py": T5_DEAD,
        "live_module.py": T5_LIVE,
        "registry.py": T5_DYNAMIC,
        "dyn_target.py": T5_DYN_TARGET,
        "main.py": T5_MAIN,
        "static_ref.py": T5_STATIC_REF,
    },
}

# Independent ground truth (planted, NOT derived from AST tooling).
# For t1: signature change of normalize_name touches all importers.
GROUND_TRUTH: dict[str, dict[str, Any]] = {
    "t1_blast": {
        "affected_files": ["utils.py", "caller.py", "caller2.py",
                           "route.py", "sub/inner.py"],
        "affected_symbols": ["normalize_name"],
        "root_symbol": "normalize_name",
    },
    "t2_api": {
        "affected_files": ["base.py", "mid.py", "leaf.py", "ser.py"],
        "affected_symbols": ["Context"],
        "root_symbol": "Context",
    },
    "t3_trace": {
        "affected_files": ["config.py"],
        "root_symbol": "load_config",
        "root_cause": "load_config returns key 'db' missing when "
                     "DB_URL unset (None propagated through build_pool "
                     "into open_connection .split)",
    },
    "t4_enum": {
        "affected_files": ["enums.py", "use_a.py", "use_b.py",
                           "use_c.py", "ser.py"],
        "affected_symbols": ["Mode"],
        "root_symbol": "Mode",
    },
    "t5_dead": {
        "dead": ["dead_module.obsolete_helper"],
        "static_reachable": ["live_module.live_fn",
                             "live_module.register",
                             "registry.register_dynamic",
                             "static_ref.unused_wrapper"],
        "dynamic_reachable": ["dyn_target.handler"],
    },
}


def build_fixture(root: Path, name: str) -> Path:
    """Create fixture package under root; returns its directory."""
    pkg = root / "pkg" / name
    pkg.mkdir(parents=True, exist_ok=True)
    for rel, text in FIXTURES[name].items():
        f = pkg / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return pkg


# ------------------------------------------------------------- Mode A

def mode_a_callers(pkg: Path, symbol: str) -> set[str]:
    """TEXT-FIRST: find files importing/using `symbol` via grep."""
    hits: set[str] = set()
    for f in sorted(pkg.rglob("*.py")):
        if any(part.startswith("__pycache__") for part in f.parts):
            continue
        text = f.read_text(encoding="utf-8")
        if re.search(rf"\b{symbol}\b", text):
            hits.add(str(f.relative_to(pkg)).replace("\\", "/"))
    return hits


def mode_a_enum_consumers(pkg: Path, enum_name: str) -> set[str]:
    """TEXT-FIRST: every file naming the enum symbol."""
    hits: set[str] = set()
    for f in sorted(pkg.rglob("*.py")):
        if any(part.startswith("__pycache__") for part in f.parts):
            continue
        text = f.read_text(encoding="utf-8")
        if re.search(rf"\b{enum_name}\b", text):
            hits.add(str(f.relative_to(pkg)).replace("\\", "/"))
    return hits


def mode_a_dead_candidates(pkg: Path) -> dict[str, str]:
    """TEXT-FIRST dead-code scan: functions defined but never referenced."""
    defined: dict[str, str] = {}
    for f in sorted(pkg.rglob("*.py")):
        if any(part.startswith("__pycache__") for part in f.parts):
            continue
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"^def (\w+)", text, re.MULTILINE):
            defined[f"{f.relative_to(pkg)}.{m.group(1)}"] = (
                f.relative_to(pkg).as_posix())
    referenced: set[str] = set()
    for f in sorted(pkg.rglob("*.py")):
        if any(part.startswith("__pycache__") for part in f.parts):
            continue
        text = f.read_text(encoding="utf-8")
        for name in defined:
            sym = name.rsplit(".", 1)[1]
            defining_file = name.rsplit(".", 1)[0]
            # the defining file's own def line is not a reference;
            # any OTHER occurrence (including imports) is.
            body = text
            if defining_file == f.relative_to(pkg).as_posix():
                body = body.replace("def " + sym, "", 1)
            if re.search(rf"\b{sym}\b", body):
                referenced.add(name)
    return {name: f for name, f in defined.items()
            if name not in referenced}


# ------------------------------------------------------------- Mode B

def mode_b_index(pkg: Path) -> tuple[Any, Any]:
    """STRUCTURAL: real ast_indexer + codegraph over fixture files."""
    from asha.ast_indexer import index_module
    from asha.codegraph import build_graph

    rels = sorted(f.relative_to(pkg).as_posix()
                  for f in pkg.rglob("*.py")
                  if "__pycache__" not in f.parts)
    indices: list[Any] = []
    bytes_read = 0
    for rel in rels:
        text = (pkg / rel).read_text(encoding="utf-8")
        bytes_read += len(text.encode("utf-8"))
        module = "pkg." + rel[:-3].replace("/", ".")
        indices.append(index_module(module, text))
    graph = build_graph(tuple(indices))
    facts = sum(len(idx.symbols) for idx in indices)
    return indices, graph, facts, bytes_read


def mode_b_closure(pkg: Path, symbol: str) -> tuple[set[str], set[str]]:
    """STRUCTURAL: reverse edge walk over the CodeGraph.

    Returns (module_set, symbol_set): every indexable module whose
    symbols have a dependency edge INTO a sym:module:symbol node for
    `symbol` (i.e. the consumers / blast radius).
    """
    indices, graph, _facts, _bytes = mode_b_index(pkg)
    target_nodes = {
        f"sym:{idx.module}:{fact.name}"
        for idx in indices
        for fact in idx.symbols
        if fact.name == symbol
    }
    modules: set[str] = set()
    symbols: set[str] = set()
    for edge in graph.edges:
        if edge.target in target_nodes:
            source_mod = edge.source.split(":")[1]
            source_mod = source_mod.removeprefix("pkg.")
            modules.add(source_mod)
            symbols.add(edge.source)
    # the defining module is part of the edit set (change the symbol
    # definition -> the defining file must be edited too)
    for idx in indices:
        for fact in idx.symbols:
            if fact.name == symbol:
                mod = idx.module
                mod = mod.removeprefix("pkg.")
                modules.add(mod)
    return modules, symbols


def mode_b_dead(pkg: Path) -> dict[str, str]:
    """STRUCTURAL dead-code scan, validated H-AST T5 methodology.

    Entry-root closure (mod:pkg.main) -> STATIC_REACHABLE;
    symbols in modules textually targeted by a dynamic-import call
    site (demonstrated dynamic boundary from index.unresolved markers)
    -> UNCERTAIN (preserved, NEVER dead);
    everything else -> DEAD_CANDIDATE.
    """
    from asha.codegraph import closure

    indices, graph, _facts, _bytes = mode_b_index(pkg)
    entry = "mod:pkg.main"
    res = closure(graph, (entry,), "pkg.main")
    reach_syms = {n for n in res.reachable if n.startswith("sym:")}
    # demonstrated dynamic boundary: modules whose unresolved markers
    # include dynamic imports
    dynamic_sources = {
        idx.module for idx in indices
        if any("dynamic_import" in m for m in idx.unresolved)
    }
    # importlib string targets recovered from the call-site text
    dynamic_targets: set[str] = set()
    for idx in indices:
        if idx.module not in dynamic_sources:
            continue
        rel = idx.module[len("pkg."):].replace(".", "/") + ".py"
        text = (pkg / rel).read_text(encoding="utf-8")
        for m in re.finditer(
                r'import_module\(["\']([\w.]+)["\']\)', text):
            dynamic_targets.add(m.group(1))
    dead: dict[str, str] = {}
    for idx in indices:
        for fact in idx.symbols:
            node = f"sym:{idx.module}:{fact.name}"
            if node in reach_syms:
                continue
            mod = idx.module.removeprefix("pkg.")
            rel = mod.replace(".", "/") + ".py"
            if idx.module in dynamic_targets:
                continue  # UNCERTAIN: preserved, never dead
            dead[f"{rel}.{fact.name}"] = rel
    return dead


# ------------------------------------------------------------- runner

def run_benchmark() -> dict[str, Any]:
    out: dict[str, Any] = {}
    # Mode B structural overhead: cold import of the AST toolchain
    t0 = time.perf_counter()
    import asha.ast_indexer
    import asha.codegraph  # noqa: F401
    out["environment"] = {
        "cold_import_ms": round((time.perf_counter() - t0) * 1000, 1),
        "index_state": "COLD per task (fixtures built fresh; no cache "
                       "persists between runs)",
    }

    with tempfile.TemporaryDirectory(prefix="hast_") as tmp:
        root = Path(tmp)

        # ------- TASK 1: BLAST RADIUS --------------------------------
        pkg = build_fixture(root, "t1_blast")
        gt = set(GROUND_TRUTH["t1_blast"]["affected_files"])
        t0 = time.perf_counter()
        a1 = mode_a_callers(pkg, "normalize_name")
        t_a1 = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        mods, _syms = mode_b_closure(pkg, "normalize_name")
        t_b1 = (time.perf_counter() - t0) * 1000
        b1 = {m.replace(".", "/") + ".py" for m in mods}
        out["t1"] = {
            "ground_truth": sorted(gt),
            "mode_a": sorted(a1),
            "mode_b": sorted(b1),
            "mode_a_recall": len(gt & a1) / len(gt),
            "mode_b_recall": len(gt & b1) / len(gt),
            "mode_a_precision": (len(gt & a1) / len(a1)
                                 if a1 else 0.0),
            "mode_b_precision": (len(gt & b1) / len(b1)
                                 if b1 else 0.0),
            "mode_a_false_pos": sorted(a1 - gt),
            "mode_b_false_pos": sorted(b1 - gt),
            "mode_a_false_neg": sorted(gt - a1),
            "mode_b_false_neg": sorted(gt - b1),
            "mode_a_ms": round(t_a1, 1),
            "mode_b_ms": round(t_b1, 1),
        }

        # ------- TASK 2: API CONTRACT PROPAGATION ---------------------
        pkg = build_fixture(root, "t2_api")
        gt = set(GROUND_TRUTH["t2_api"]["affected_files"])
        t0 = time.perf_counter()
        a2 = mode_a_callers(pkg, "Context")
        t_a2 = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        mods, _syms = mode_b_closure(pkg, "Context")
        t_b2 = (time.perf_counter() - t0) * 1000
        b2 = {m.replace(".", "/") + ".py" for m in mods}
        out["t2"] = {
            "ground_truth": sorted(gt),
            "mode_a": sorted(a2),
            "mode_b": sorted(b2),
            "mode_a_recall": len(gt & a2) / len(gt),
            "mode_b_recall": len(gt & b2) / len(gt),
            "mode_a_precision": (len(gt & a2) / len(a2)
                                 if a2 else 0.0),
            "mode_b_precision": (len(gt & b2) / len(b2)
                                 if b2 else 0.0),
            "mode_a_false_pos": sorted(a2 - gt),
            "mode_b_false_pos": sorted(b2 - gt),
            "mode_a_false_neg": sorted(gt - a2),
            "mode_b_false_neg": sorted(gt - b2),
            "mode_a_ms": round(t_a2, 1),
            "mode_b_ms": round(t_b2, 1),
        }

        # ------- TASK 3: DEEP TRACE -----------------------------------
        pkg = build_fixture(root, "t3_trace")
        # Mode A: text trace of the 'db' key through the chain
        t0 = time.perf_counter()
        a3_hits = mode_a_callers(pkg, "db")
        t_a3 = (time.perf_counter() - t0) * 1000
        # normalized: any file whose basename matches an affected file
        gt3 = set(GROUND_TRUTH["t3_trace"]["affected_files"])
        # Mode B: closure from load_config
        t0 = time.perf_counter()
        mods, _syms = mode_b_closure(pkg, "load_config")
        t_b3 = (time.perf_counter() - t0) * 1000
        b3 = {m.replace(".", "/") + ".py" for m in mods}
        a3_norm = set()
        for hit in a3_hits:
            for g in gt3:
                if Path(hit).name == g:
                    a3_norm.add(g)
        out["t3"] = {
            "ground_truth": sorted(gt3),
            "root_cause": GROUND_TRUTH["t3_trace"]["root_cause"],
            "mode_a": sorted(a3_hits),
            "mode_a_normalized": sorted(a3_norm),
            "mode_b": sorted(b3),
            "mode_b_recall": len(gt3 & b3) / len(gt3),
            "mode_a_recall": (len(gt3 & a3_norm) / len(gt3)
                              if gt3 else 0.0),
            "mode_a_ms": round(t_a3, 1),
            "mode_b_ms": round(t_b3, 1),
        }

        # ------- TASK 4: CROSS-MODULE ENUM ----------------------------
        pkg = build_fixture(root, "t4_enum")
        gt = set(GROUND_TRUTH["t4_enum"]["affected_files"])
        t0 = time.perf_counter()
        a4 = mode_a_enum_consumers(pkg, "Mode")
        t_a4 = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        mods, _syms = mode_b_closure(pkg, "Mode")
        t_b4 = (time.perf_counter() - t0) * 1000
        b4 = {m.replace(".", "/") + ".py" for m in mods}
        out["t4"] = {
            "ground_truth": sorted(gt),
            "mode_a": sorted(a4),
            "mode_b": sorted(b4),
            "mode_a_recall": len(gt & a4) / len(gt),
            "mode_b_recall": len(gt & b4) / len(gt),
            "mode_a_precision": (len(gt & a4) / len(a4)
                                 if a4 else 0.0),
            "mode_b_precision": (len(gt & b4) / len(b4)
                                 if b4 else 0.0),
            "mode_a_false_pos": sorted(a4 - gt),
            "mode_b_false_pos": sorted(b4 - gt),
            "mode_a_false_neg": sorted(gt - a4),
            "mode_b_false_neg": sorted(gt - b4),
            "mode_a_ms": round(t_a4, 1),
            "mode_b_ms": round(t_b4, 1),
        }

        # ------- TASK 5: DEAD CODE ------------------------------------
        pkg = build_fixture(root, "t5_dead")
        t0 = time.perf_counter()
        a5 = mode_a_dead_candidates(pkg)
        t_a5 = (time.perf_counter() - t0) * 1000
        gt_dead = set(GROUND_TRUTH["t5_dead"]["dead"])
        gt_static = set(GROUND_TRUTH["t5_dead"]["static_reachable"])
        gt_dynamic = set(GROUND_TRUTH["t5_dead"]["dynamic_reachable"])
        a5_keys = set(a5)
        # normalize: 'dead_module.py.obsolete_helper' -> gt form
        a5_norm = {name.replace(".py.", "."): rel
                   for name, rel in a5.items()}
        t0 = time.perf_counter()
        b5 = mode_b_dead(pkg)
        t_b5 = (time.perf_counter() - t0) * 1000
        b5_keys = set(b5)
        b5_norm = {name.replace(".py.", "."): rel
                   for name, rel in b5.items()}
        out["t5"] = {
            "dead_ground_truth": sorted(gt_dead),
            "static_ground_truth": sorted(gt_static),
            "dynamic_ground_truth": sorted(gt_dynamic),
            "mode_a_dead_hits": sorted(a5_keys),
            "mode_a_true_dead": sorted(set(a5_norm) & gt_dead),
            "mode_a_false_pos": sorted(set(a5_norm) - gt_dead - gt_static),
            "mode_a_missed": sorted(gt_dead - set(a5_norm)),
            "mode_b_dead_hits": sorted(b5_keys),
            "mode_b_true_dead": sorted(set(b5_norm) & gt_dead),
            "mode_b_false_pos": sorted(set(b5_norm) - gt_dead - gt_static),
            "mode_b_missed": sorted(gt_dead - set(b5_norm)),
            "mode_b_dynamic_survives":
                not any(k.startswith("dyn_target") for k in b5),
            # dynamic target: importlib name not statically resolvable
            "dynamic_preserved": "dyn_target.handler"
                not in a5_keys,
            "mode_a_ms": round(t_a5, 1),
            "mode_b_ms": round(t_b5, 1),
        }

    return out


def main() -> int:
    res = run_benchmark()
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())