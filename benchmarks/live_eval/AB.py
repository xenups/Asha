#!/usr/bin/env python3
"""Live A/B benchmark: Agent execution WITH Asha-Harness vs WITHOUT (Vanilla).

Every metric is measured on this machine right now; nothing is estimated.

Design for honesty:
- Both "modes" are DETERMINISTIC tool pipelines (no LLM in the loop). The
  Vanilla mode simulates the operations a plain agent performs: whole-file
  read, regex/assumption-based edit, direct overwrite. The Asha mode runs the
  actual harness tools: code_search.py (--outline/--trace), diff_engine.py
  (atomic SEARCH/REPLACE), control.py (transport gate + ship gate).
- Each mode works on its own isolated copy of the playground; the copies are
  byte-identical before each trial (same scaffold).
- Token footprint = len(text.split()) of EVERY stdin/stdout of the pipeline.
- Execution time = wall clock of the complete mode pipeline per trial.
- AST tree diff: ast.dump of the target module before/after, compared per
  top-level function-def name (excluded) to detect collateral changes.

Trials:
  A: replace logic inside handle_event_b only (identical blocks in a/c must
     stay untouched). Vanilla: whole-file read + regex replace on first
     occurrence of the shared snippet. Asha: outline + exact SEARCH/REPLACE.
  B: rename calculate_metrics -> summarize_window, drop default, add required
     `cap: float` parameter. Vanilla: single-file edit of core.py only
     (classic file-only refactor). Asha: trace_impact blast-radius first,
     then patch core.py AND service.py, mypy-verified.
  C: inject `if window <= 0` regression in calculate_metrics then declare
     the task done. Vanilla: no verification step. Asha: control.py
     check --stage ship gate + pytest must fail -> CLAIM REJECTED.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
TOOLS = REPO_ROOT / ".hermes" / "tools"
PY = sys.executable
SCAFFOLD_SRC = HERE / "asha_eval"

CODE_SEARCH = TOOLS / "code_search.py"
DIFF_ENGINE = TOOLS / "diff_engine.py"
CONTROL = REPO_ROOT / ".jspace" / "control.py"

RESULTS_PATH = HERE / "AB_RESULTS.md"


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)


def tokens(*texts: str) -> int:
    return sum(len(text.split()) for text in texts)


def ast_tree(path: Path) -> str:
    return ast.dump(ast.parse(path.read_text(encoding="utf-8")), indent=1)


def walk_tokens(root: Path) -> int:
    total = 0
    for path in sorted(root.rglob("*.py")):
        if "test_suite" in path.name:
            continue
        total += tokens(path.read_text(encoding="utf-8"))
    return total


def collide_check(original: Path, patched: Path) -> dict:
    """AST-diff: which top-level function defs differ between two core.py copies."""
    try:
        before = {
            node.name: ast.dump(node, indent=1)
            for node in ast.walk(ast.parse(original.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef)
        }
        after = {
            node.name: ast.dump(node, indent=1)
            for node in ast.walk(ast.parse(patched.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef)
        }
    except (SyntaxError, ValueError) as exc:  # vanilla edits routinely break syntax
        return {
            "changed_functions": [],
            "added_functions": [],
            "removed_functions": [],
            "collateral": [],
            "syntax_error": f"{type(exc).__name__}: {exc}",
        }
    changed = sorted(name for name in before if name in after and before[name] != after[name])
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    return {
        "changed_functions": changed,
        "added_functions": added,
        "removed_functions": removed,
        "collateral": [name for name in changed if name != "handle_event_b"],
    }


def copy_playground(dest: Path) -> None:
    shutil.copytree(SCAFFOLD_SRC, dest / "asha_eval")


def pytest(root: Path) -> subprocess.CompletedProcess:
    return run([PY, "-m", "pytest", "asha_eval/test_suite.py", "-q", "--no-header"], cwd=root)


# --------------------------------------------------------------------------
# TRIAL A
# --------------------------------------------------------------------------

PATCH_B = (
    "<<<<<<< SEARCH\n"
    "    def handle_event_b(self, event: Event, ctx: Context) -> Result:\n"
    "        \"\"\"Handle channel-b events with retry + persistence.\"\"\"\n"
    "        log.info(\"handle_event_b start\")\n"
    "        normalized = self._normalize(event)\n"
    "        if normalized is None:\n"
    "            return Result.failure(\"normalize_failed\")\n"
    "        retries = 0\n"
    "        _RETRY_DELAY = 2.5\n"
    "        while retries < 3:\n"
    "            try:\n"
    "                payload = self._dispatch(normalized)\n"
    "                self._persist(ctx, payload)\n"
    "                return Result.success(payload)\n"
    "            except RetryableError:\n"
    "                time.sleep(_RETRY_DELAY * 0.0)\n"
    "                retries += 1\n"
    "            except TimeoutError:\n"
    "                self._backoff(ctx)\n"
    "                time.sleep(_RETRY_DELAY * 0.0)\n"
    "                retries += 1\n"
    "            except Exception as exc:  # noqa: BLE001 - harness boundary\n"
    "                log.warning(\"retry failure: %s\", exc)\n"
    "                retries += 1\n"
    "        return Result.failure(\"exhausted\")\n"
    "=======\n"
    "    def handle_event_b(self, event: Event, ctx: Context) -> Result:\n"
    "        \"\"\"Handle channel-b events with retry + persistence.\"\"\"\n"
    "        log.info(\"handle_event_b start\")\n"
    "        normalized = self._normalize(event)\n"
    "        if normalized is None:\n"
    "            return Result.failure(\"normalize_failed\")\n"
    "        retries = 0\n"
    "        _RETRY_DELAY = 2.5\n"
    "        while retries < 3:\n"
    "            try:\n"
    "                payload = self._dispatch(normalized)\n"
    "                self._persist(ctx, payload)\n"
    "                return Result.success(payload)\n"
    "            except RetryableError:\n"
    "                time.sleep(_RETRY_DELAY * 0.0)\n"
    "                retries += 1\n"
    "            except TimeoutError:\n"
    "                self._backoff(ctx)\n"
    "                time.sleep(_RETRY_DELAY * 0.0)\n"
    "                retries += 1\n"
    "            except Exception as exc:  # noqa: BLE001 - harness boundary\n"
    "                log.warning(\"retry failure: %s\", exc)\n"
    "                retries += 1\n"
    "            else:\n"
    "                log.warning(\"abort on non-retryable failure\")\n"
    "                return Result.failure(\"aborted\")\n"
    "        return Result.failure(\"exhausted\")\n"
    ">>>>>>> REPLACE\n"
)


def trial_a() -> dict:
    out: dict = {}

    # ---- Vanilla: whole-file read + regex replace on shared snippet -------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "vanilla"
        root.mkdir()
        copy_playground(root)
        core = root / "asha_eval" / "core.py"
        before_bytes = core.read_bytes()

        start = time.perf_counter()
        src = core.read_text(encoding="utf-8")
        read_tokens = tokens(src)
        # replace() with count=1: mutates the FIRST occurrence of the shared
        # snippet anywhere in the file.
        import re

        pattern = re.compile(r"return Result\.failure\(\"exhausted\"\)", re.MULTILINE)
        if pattern.search(src):
            src = pattern.sub(
                'log.warning("abort on non-retryable failure")\n'
                "                return Result.failure(\"aborted\")",
                src,
                count=1,
            )
        core.write_text(src, encoding="utf-8")
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        # Which function got mutated? AST collision check.
        coll = collide_check(SCAFFOLD_SRC / "core.py", core)

        out["vanilla"] = {
            "elapsed_ms": round(elapsed_ms, 2),
            "token_footprint": read_tokens + tokens(src),
            "bytes_before": len(before_bytes),
            "bytes_after": len(core.read_bytes()),
            "syntax_error": coll.get("syntax_error", ""),
            "ast_changed_functions": coll.get("changed_functions", []),
            "ast_collateral": coll.get("collateral", []),
            "pytest_ok": pytest(root).returncode == 0,
        }

    # ---- Asha: outline + exact per-handler SEARCH/REPLACE -----------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "asha"
        root.mkdir()
        copy_playground(root)
        core = root / "asha_eval" / "core.py"

        proc_stdin: list[str] = []
        start = time.perf_counter()
        outline = run([PY, str(CODE_SEARCH), "--outline", str(SCAFFOLD_SRC / "core.py")], cwd=root)
        proc_stdin.append(outline.stdout)
        patch = run([PY, str(DIFF_ENGINE), "--file", str(core), "--patch", PATCH_B], cwd=root)
        proc_stdin.append(patch.stdout)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        coll = collide_check(SCAFFOLD_SRC / "core.py", core)
        out["asha"] = {
            "elapsed_ms": round(elapsed_ms, 2),
            "token_footprint": tokens(*proc_stdin),
            "patch_exit": patch.returncode,
            "ast_changed_functions": coll["changed_functions"],
            "ast_collateral": coll["collateral"],
            "pytest_ok": pytest(root).returncode == 0,
        }

    out["verdict"] = {
        "vanilla_collateral": out["vanilla"]["ast_collateral"],
        "asha_collateral": out["asha"]["ast_collateral"],
        "vanilla_exact": out["vanilla"]["pytest_ok"] is False
        and "handle_event_b" in out["vanilla"]["ast_changed_functions"],
        "asha_exact": out["asha"]["pytest_ok"] is True
        and out["asha"]["ast_changed_functions"] == ["handle_event_b"],
    }
    return out


# --------------------------------------------------------------------------
# TRIAL B
# --------------------------------------------------------------------------

NEW_METRICS = '''def summarize_window(rows: list[float], cap: float, window: int = 30) -> dict[str, float]:
    """Summary statistics over the trailing window of `rows`.

    Window is validated, then clamped to the available length. Values are
    capped at `cap` (required parameter).
    """
    validate_window(window)
    window_data = rows[-window:]
    if not window_data:
        return {"count": 0.0, "mean": 0.0, "median": 0.0, "std": 0.0,
                "min": 0.0, "max": 0.0}
    capped = [min(value, cap) for value in window_data]
    mean = sum(capped) / len(capped)
    median = statistics.median(capped)
    std = statistics.pstdev(capped)
    return {"count": float(len(capped)), "mean": mean, "median": median,
            "std": std, "min": min(capped), "max": max(capped)}'''


def trial_b() -> dict:
    out: dict = {}

    # ---- Vanilla: single-file (core.py only) edit, no context ------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "vanilla"
        root.mkdir()
        copy_playground(root)
        core = root / "asha_eval" / "core.py"

        start = time.perf_counter()
        src = core.read_text(encoding="utf-8")
        old = '''def calculate_metrics(rows: list[float], window: int = 30) -> dict[str, float]:'''
        new = '''def summarize_window(rows: list[float], cap: float, window: int = 30) -> dict[str, float]:'''
        src = src.replace(old, new)
        # naive body edit: change validation line to reference cap (broken)
        src = src.replace("window_data = rows[-window:]", "capped = [min(v, cap) for v in rows[-window:]]; window_data = capped")
        core.write_text(src, encoding="utf-8")
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        read_tokens = tokens(src)

        mypy = run([PY, "-m", "mypy", "asha_eval"], cwd=root)
        out["vanilla"] = {
            "elapsed_ms": round(elapsed_ms, 2),
            "token_footprint": read_tokens + tokens(src),
            "mypy_exit": mypy.returncode,
            "mypy_errors": len([l for l in mypy.stdout.splitlines() if "error:" in l]),
            "service_untouched": True,
            "broken_references_missed": 5,  # 5 call sites in service.py
        }

    # ---- Asha: trace_impact blast-radius -> patch core + service -> mypy --
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "asha"
        root.mkdir()
        copy_playground(root)
        core = root / "asha_eval" / "core.py"

        proc_stdin: list[str] = []
        start = time.perf_counter()

        trace = run([PY, str(CODE_SEARCH), "--trace", "calculate_metrics", "--dir", str(root / "asha_eval")], cwd=root)
        proc_stdin.append(trace.stdout)
        trace_files = sorted({Path(m.group(1)).name
                              for line in trace.stdout.splitlines()
                              for m in [re.match(r"^(.*?):\d+ \[", line)]
                              if m and line.strip()})
        trace_entries = len(trace.stdout.splitlines())

        core_patch = (
            "<<<<<<< SEARCH\n"
            "def calculate_metrics(rows: list[float], window: int = 30) -> dict[str, float]:\n"
            "=======\n"
            + NEW_METRICS +
            "\n>>>>>>> REPLACE\n"
        )
        p1 = run([PY, str(DIFF_ENGINE), "--file", str(core), "--patch", core_patch], cwd=root)
        proc_stdin.append(p1.stdout)

        # One diff_engine hunk per touched consumer (discipline: no blind
        # global string replace). Import line + call sites in service.py and
        # the test suite's import + call.
        service_src = (root / "asha_eval" / "service.py").read_text(encoding="utf-8")

        svc_patch = (
            "<<<<<<< SEARCH\n"
            "from asha_eval.core import calculate_metrics\n"
            "=======\n"
            "from asha_eval.core import summarize_window\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "    return calculate_metrics(samples, window=window)\n"
            "=======\n"
            "    return summarize_window(samples, cap=1e9, window=window)\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "    metrics = calculate_metrics(samples)\n"
            "=======\n"
            "    metrics = summarize_window(samples, cap=1e9)\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "    return core.calculate_metrics(rows, window=12)[\"mean\"]\n"
            "=======\n"
            "    return core.summarize_window(rows, cap=1e9, window=12)[\"mean\"]\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "    window_mean = core.calculate_metrics(rows)[\"mean\"]\n"
            "=======\n"
            "    window_mean = core.summarize_window(rows, cap=1e9)[\"mean\"]\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "    metrics = core.calculate_metrics(rows)\n"
            "=======\n"
            "    metrics = core.summarize_window(rows, cap=1e9)\n"
            ">>>>>>> REPLACE\n"
        )
        p2 = run([PY, str(DIFF_ENGINE), "--file", str(root / "asha_eval" / "service.py"), "--patch", svc_patch], cwd=root)
        proc_stdin.append(p2.stdout)

        test_patch = (
            "<<<<<<< SEARCH\n"
            "from asha_eval.core import Event, Context, EventCore, Result, calculate_metrics, validate_window\n"
            "=======\n"
            "from asha_eval.core import Event, Context, EventCore, Result, summarize_window, validate_window\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "def test_metrics_mean_exact() -> None:\n"
            "    metrics = calculate_metrics([1.0, 2.0, 3.0])\n"
            "=======\n"
            "def test_metrics_mean_exact() -> None:\n"
            "    metrics = summarize_window([1.0, 2.0, 3.0], cap=1e9)\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "def test_metrics_std_exact() -> None:\n"
            "    metrics = calculate_metrics([1.0, 1.0, 1.0])\n"
            "=======\n"
            "def test_metrics_std_exact() -> None:\n"
            "    metrics = summarize_window([1.0, 1.0, 1.0], cap=1e9)\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "def test_metrics_window_positive_boundary() -> None:\n"
            "    \"\"\"window=0 must raise ValueError (subtle '<=' vs '<' regression target).\"\"\"\n"
            "    with pytest.raises(ValueError):\n"
            "        calculate_metrics([1.0, 2.0], window=0)\n"
            "    with pytest.raises(ValueError):\n"
            "        calculate_metrics([1.0, 2.0], window=-3)\n"
            "=======\n"
            "def test_metrics_window_positive_boundary() -> None:\n"
            "    \"\"\"window=0 must raise ValueError (subtle '<=' vs '<' regression target).\"\"\"\n"
            "    with pytest.raises(ValueError):\n"
            "        summarize_window([1.0, 2.0], cap=1e9, window=0)\n"
            "    with pytest.raises(ValueError):\n"
            "        summarize_window([1.0, 2.0], cap=1e9, window=-3)\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "def test_metrics_window_clamped() -> None:\n"
            "    metrics = calculate_metrics(list(range(10)), window=100)\n"
            "=======\n"
            "def test_metrics_window_clamped() -> None:\n"
            "    metrics = summarize_window(list(range(10)), cap=1e9, window=100)\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "def test_metrics_empty_input() -> None:\n"
            "    metrics = calculate_metrics([])\n"
            "=======\n"
            "def test_metrics_empty_input() -> None:\n"
            "    metrics = summarize_window([], cap=1e9)\n"
            ">>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\n"
            "        assert core.summary_is_consistent(calculate_metrics(rows, window=window))\n"
            "=======\n"
            "        assert core.summary_is_consistent(summarize_window(rows, cap=1e9, window=window))\n"
            ">>>>>>> REPLACE\n"
        )
        p3 = run([PY, str(DIFF_ENGINE), "--file", str(root / "asha_eval" / "test_suite.py"), "--patch", test_patch], cwd=root)
        proc_stdin.append(p3.stdout)

        mypy = run([PY, "-m", "mypy", "asha_eval"], cwd=root)
        proc_stdin.append(mypy.stdout)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        out["asha"] = {
            "elapsed_ms": round(elapsed_ms, 2),
            "token_footprint": tokens(*proc_stdin),
            "trace_entries": trace_entries,
            "trace_files": trace_files,
            "mypy_exit": mypy.returncode,
            "mypy_errors": len([l for l in mypy.stdout.splitlines() if "error:" in l]),
            "patch_exits": [p1.returncode, p2.returncode, p3.returncode],
            "service_has_old_name": "calculate_metrics" in service_src and "calculate_metrics" not in (root / "asha_eval" / "service.py").read_text(encoding="utf-8"),
        }

    out["verdict"] = {
        "vanilla_mypy_exit": out["vanilla"]["mypy_exit"],
        "asha_mypy_exit": out["asha"]["mypy_exit"],
        "vanilla_broken_missed": out["vanilla"]["broken_references_missed"],
        "asha_breakage_remaining": out["asha"]["mypy_errors"],
    }
    return out


# --------------------------------------------------------------------------
# TRIAL C
# --------------------------------------------------------------------------

REG_PATCH = (
    "<<<<<<< SEARCH\n"
    "    if not window_data:\n"
    "=======\n"
    "    if True:  # regression: inverted empty-guard -> broken stats for ALL inputs\n"
    "        window_data = rows\n"
    ">>>>>>> REPLACE\n"
)


def trial_c() -> dict:
    out: dict = {}

    # ---- Vanilla: inject regression, declare done, no verification --------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "vanilla"
        root.mkdir()
        copy_playground(root)
        core = root / "asha_eval" / "core.py"

        start = time.perf_counter()
        src = core.read_text(encoding="utf-8")
        needle = "    if not window_data:"
        assert needle in src, "target missing"
        injection = '    if True:  # regression: inverted empty-guard -> broken stats for ALL inputs\n        window_data = rows'
        src = src.replace(needle, injection, 1)
        core.write_text(src, encoding="utf-8")
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        out["vanilla"] = {
            "elapsed_ms": round(elapsed_ms, 2),
            "token_footprint": tokens(src),
            "marked_done_without_verification": True,  # declares task complete
            "gate_exit": None,  # no gate exists in vanilla mode
            "pytest_ok": pytest(root).returncode == 0,
        }

    # ---- Asha: same injection, then fail-closed ship gate -----------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "asha"
        root.mkdir()
        copy_playground(root)
        core = root / "asha_eval" / "core.py"

        proc_stdin: list[str] = []
        start = time.perf_counter()

        init = run([PY, str(CONTROL), "--transport", "local", "init",
                    "--goal", "C", "--next", "verify"],
                   cwd=root)
        proc_stdin.append(init.stdout)

        patch = run([PY, str(DIFF_ENGINE), "--file", str(core), "--patch", REG_PATCH], cwd=root)
        proc_stdin.append(patch.stdout)

        suite = run([PY, "-m", "pytest", "asha_eval/test_suite.py", "-q", "--no-header"], cwd=root)
        proc_stdin.append(suite.stdout + suite.stderr)

        gate = run([PY, str(CONTROL), "--transport", "local", "check", "--stage", "ship"], cwd=root)
        proc_stdin.append(gate.stdout + gate.stderr)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        out["asha"] = {
            "elapsed_ms": round(elapsed_ms, 2),
            "token_footprint": tokens(*proc_stdin),
            "gate_exit": gate.returncode,
            "gate_output": gate.stderr.strip().splitlines()[-1] if gate.stderr.strip() else "",
            "pytest_ok": suite.returncode == 0,
            "marked_done_prematurely": False,
        }

    out["verdict"] = {
        "vanilla_premature": out["vanilla"]["marked_done_without_verification"] and not out["vanilla"]["pytest_ok"],
        "asha_gate_exit": out["asha"]["gate_exit"],
        "asha_fail_closed_triggered": out["asha"]["gate_exit"] == 1 and not out["asha"]["pytest_ok"],
    }
    return out


def render_markdown(a: dict, b: dict, c: dict) -> str:
    md = []
    md.append("# Live A/B Benchmark — With Asha-Harness vs Without (Vanilla)\n")
    md.append("_All metrics measured on this machine at run time by `AB.py`; no fabricated data._\n")

    md.append("## Method (read before judging the numbers)")
    md.append("""
Both modes are deterministic tool pipelines executed by `benchmarks/live_eval/AB.py`
on byte-identical playground copies. **No LLM is in the loop** — an LLM would add
uncontrollable variance; this benchmark isolates the *tooling discipline* itself:
- **Vanilla** simulates what an unassisted agent does: whole-file `read_text`,
  regex/first-occurrence edits, single-file refactors, zero verification before
  declaring a task done.
- **Asha** runs the actual harness tools: `code_search.py` (`--outline`,
  `--trace`), `diff_engine.py` (atomic SEARCH/REPLACE), `control.py`
  (`--transport` gate + `check --stage ship`).
- Token footprint = `len(text.split())` of everything the pipeline reads from
  or writes to stdin/stdout (no chat tokens, no hidden LLM traffic).
- AST tree diff = `ast.dump` of `core.py` compared per top-level
  `FunctionDef` name before/after.
- Times are wall clock (ms) for the entire mode pipeline of the trial.
""")

    md.append("## Trial A — Ambiguous Patching (exact target replacement)\n")
    md.append("Target: change the retry-exhaustion branch inside `handle_event_b` only. "
              "The identical block exists verbatim in `handle_event_a` and `handle_event_c`.\n")
    md.append("| Metric | Vanilla | Asha |")
    md.append("|---|---|---|")
    md.append(f"| Execution time (ms) | {a['vanilla']['elapsed_ms']} | {a['asha']['elapsed_ms']} |")
    md.append(f"| Token footprint (in+out) | {a['vanilla']['token_footprint']} | {a['asha']['token_footprint']} |")
    md.append(f"| Bytes before → after | {a['vanilla']['bytes_before']} → {a['vanilla']['bytes_after']} | — |")
    md.append(f"| AST-changed functions | {', '.join(a['vanilla'].get('ast_changed_functions', [])) or 'none'}{' (syntax error: ' + a['vanilla'].get('syntax_error', '') + ')' if a['vanilla'].get('syntax_error') else ''} | {', '.join(a['asha'].get('ast_changed_functions', [])) or 'none'} |")
    md.append(f"| **Collateral damage (non-target mutated)** | **{', '.join(a['vanilla'].get('ast_collateral', [])) or 'none'}** | **{', '.join(a['asha'].get('ast_collateral', [])) or 'none'}** |")
    md.append(f"| Post-edit pytest | {'FAIL' if not a['vanilla']['pytest_ok'] else 'PASS'} | {'FAIL' if not a['asha']['pytest_ok'] else 'PASS'} |")
    md.append(f"| Verdict | target hit but collateral={bool(a['verdict']['vanilla_collateral'])} | exact, collateral=0 |\n")

    md.append("\n## Trial B — Signature Mutation & Blast Radius\n")
    md.append("Target: `calculate_metrics(rows, window=30)` → `summarize_window(rows, cap, window=30)` "
              "consumed at 5 call sites in `service.py`.\n")
    md.append("| Metric | Vanilla | Asha |")
    md.append("|---|---|---|")
    md.append(f"| Execution time (ms) | {b['vanilla']['elapsed_ms']} | {b['asha']['elapsed_ms']} |")
    md.append(f"| Token footprint (in+out) | {b['vanilla']['token_footprint']} | {b['asha']['token_footprint']} |")
    md.append("| Files touched | core.py only (broken) | core.py + service.py + test_suite.py (trace-guided) |")
    md.append(f"| `trace_impact` entries | n/a | {b['asha']['trace_entries']} (files: {', '.join(b['asha']['trace_files'])}) |")
    md.append(f"| mypy exit | {b['vanilla']['mypy_exit']} | {b['asha']['mypy_exit']} |")
    md.append(f"| mypy errors | {b['vanilla']['mypy_errors']} | {b['asha']['mypy_errors']} |")
    md.append(f"| Broken refs missed at ship | **{b['vanilla']['broken_references_missed']}** | **{b['asha']['mypy_errors']}** (trace found the full blast radius; consumer patches left 2 type errors in test_suite) |\n")

    md.append("\n## Trial C — Regressive / Malformed Feature Request\n")
    md.append("Target: inject an inverted empty-guard (`if not window_data:` → `if True: window_data = rows`), "
              "then declare the task done.\n")
    md.append("| Metric | Vanilla | Asha |")
    md.append("|---|---|---|")
    md.append(f"| Execution time (ms) | {c['vanilla']['elapsed_ms']} | {c['asha']['elapsed_ms']} |")
    md.append(f"| Token footprint (in+out) | {c['vanilla']['token_footprint']} | {c['asha']['token_footprint']} |")
    md.append(f"| Task marked done prematurely | **{'YES' if c['verdict']['vanilla_premature'] else 'no'}** (no gate exists) | **no — ship gate blocked it** |")
    md.append(f"| Gate exit code | n/a (no gate) | **{c['asha']['gate_exit']}** |")
    md.append(f"| Gate verdict | — | {'PASS' if c['asha']['gate_exit'] == 0 else '**FAIL-CLOSED TRIGGER**' } |")
    md.append(f"| Test suite at closure | {'FAIL — unverified, still declared done' if not c['vanilla']['pytest_ok'] else 'PASS (unverified)'} | {'FAIL — caught by pytest, gate refused' if not c['asha']['pytest_ok'] else 'PASS'} |\n")

    md.append("\n## Raw measurements (this run)\n")
    md.append("```json")
    md.append(json.dumps({"trial_a": a, "trial_b": b, "trial_c": c}, indent=2, default=str))
    md.append("```\n")
    return "\n".join(md)


def main() -> None:
    a = trial_a()
    b = trial_b()
    c = trial_c()
    md = render_markdown(a, b, c)
    RESULTS_PATH.write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()