#!/usr/bin/env python3
"""Empirical benchmark suite for the Hermes Disciplined Harness.

Measures (all real, on this machine):
  1. AST outline vs raw read: token reduction % + parse latency (ms, median)
  2. diff_engine atomic patch latency (ms) + fail-closed rejection
  3. control.py --transport gate: fail-closed assertions (exit codes)
  4. Zero-daemon verification: listening-port delta across tool runs

Stdlib only. Run with the harness venv python so code_search.py has its
pinned tree-sitter/ast-grep stack:

    .hermes/venv/python benchmarks/bench.py [--json]

Writes benchmarks/results.json and prints a Markdown table.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = sys.executable  # run me with the harness venv interpreter
CODE_SEARCH = ROOT / "asha" / "code_search.py"
DIFF_ENGINE = ROOT / "asha" / "diff_engine.py"
CONTROL = ROOT / ".jspace" / "control.py"
TARGET_FILE = ROOT / ".jspace" / "control.py"  # 875-line real sca file

VALID_PATCH = (
    "<<<<<<< SEARCH\n"
    "needle here\n"
    "=======\n"
    "replaced needle\n"
    ">>>>>>> REPLACE\n"
)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)


def median_ms(fn, n: int = 5) -> tuple[float, list[float]]:
    samples = []
    for _ in range(n):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    assert result.returncode == 0, result.stderr
    return round(statistics.median(samples), 2), samples


def listening_ports() -> set[str]:
    """Listening TCP ports, cross-platform (netstat on Windows, ss on POSIX)."""
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=30
        )
        lines = proc.stdout.splitlines()
        pattern = re.compile(r"TCP\s+\S+:(\d+)\s+\S+\s+LISTENING", re.IGNORECASE)
        return {m.group(1) for line in lines for m in pattern.finditer(line)}
    except (OSError, subprocess.TimeoutExpired):
        proc = subprocess.run(["ss", "-tln"], capture_output=True, text=True, timeout=30)
        pattern = re.compile(r":(\d+)\s+\S+\s+LISTEN")
        return {m.group(1) for line in proc.stdout.splitlines() for m in pattern.finditer(line)}


def bench_outline() -> dict:
    src = TARGET_FILE.read_text(encoding="utf-8")
    raw_lines = len(src.splitlines())
    raw_tokens = len(src.split())
    latency, samples = median_ms(
        lambda: run([VENV_PY, str(CODE_SEARCH), "--outline", str(TARGET_FILE)]), n=5
    )
    outline = run([VENV_PY, str(CODE_SEARCH), "--outline", str(TARGET_FILE)]).stdout
    outline_lines = len(outline.splitlines())
    outline_tokens = len(outline.split())
    reduction = 1.0 - outline_tokens / raw_tokens
    return {
        "benchmark": "AST outline vs raw read",
        "file": str(TARGET_FILE.relative_to(ROOT)),
        "raw_lines": raw_lines,
        "raw_tokens": raw_tokens,
        "outline_lines": outline_lines,
        "outline_tokens": outline_tokens,
        "token_reduction_pct": round(reduction * 100, 2),
        "parse_median_ms": latency,
        "samples_ms": samples,
    }


def bench_diff() -> dict:
    with tempfile.TemporaryDirectory() as td:
        victim = Path(td) / "target.txt"

        def patch_once() -> subprocess.CompletedProcess:
            victim.write_text("line one\nneedle here\nline three\n", encoding="utf-8")
            return run([VENV_PY, str(DIFF_ENGINE), "--file", str(victim), "--patch", VALID_PATCH])

        latency, _ = median_ms(patch_once, n=5)
        # Rollback: the inverse hunk restores the original bytes.
        inverse = (
            "<<<<<<< SEARCH\n"
            "replaced needle\n"
            "=======\n"
            "needle here\n"
            ">>>>>>> REPLACE\n"
        )

        def rollback_once() -> subprocess.CompletedProcess:
            victim.write_text("line one\nreplaced needle\nline three\n", encoding="utf-8")
            return run([VENV_PY, str(DIFF_ENGINE), "--file", str(victim), "--patch", inverse])

        rollback_ms, _ = median_ms(rollback_once, n=5)
        # Fail-closed rejection: mismatched SEARCH, exit 1, zero corruption.
        victim.write_text("line one\nneedle here\nline three\n", encoding="utf-8")
        before = victim.read_text(encoding="utf-8")
        start = time.perf_counter()
        bad = run([VENV_PY, str(DIFF_ENGINE), "--file", str(victim),
                   "--patch", VALID_PATCH.replace("needle here", "absent line")])
        reject_ms = round((time.perf_counter() - start) * 1000.0, 2)
        unchanged = victim.read_text(encoding="utf-8") == before
        no_tmp = list(victim.parent.glob("*.tmp")) == []
        assert bad.returncode == 1 and unchanged and no_tmp
    return {
        "benchmark": "diff_engine atomic SEARCH/REPLACE",
        "patch_median_ms": latency,
        "rollback_median_ms": rollback_ms,
        "rejection_exit_code": bad.returncode,
        "rejection_ms": reject_ms,
        "zero_corruption": unchanged and no_tmp,
    }


def bench_transport() -> dict:
    results = {}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # Omission must fail closed, before any ledger write.
        omitted = run([VENV_PY, str(CONTROL), "init", "--goal", "g", "--next", "n"], cwd=root)
        results["omitted_exit_code"] = omitted.returncode
        results["omitted_ledger_written"] = (root / ".jspace" / "control.json").exists()
        # Explicit local transport must succeed and pin the ledger.
        ok = run([VENV_PY, str(CONTROL), "--transport", "local", "init", "--goal", "g", "--next", "n"], cwd=root)
        results["local_exit_code"] = ok.returncode
        state = json.loads((root / ".jspace" / "control.json").read_text(encoding="utf-8"))
        results["ledger_transport"] = state["transport"]
        # Transport mixing must be refused.
        mixed = run([VENV_PY, str(CONTROL), "--transport", "ssh", "status"], cwd=root)
        results["mixed_exit_code"] = mixed.returncode
    return {
        "benchmark": "control.py fail-closed transport gate",
        **results,
    }


def bench_ports() -> dict:
    before = listening_ports()
    with tempfile.TemporaryDirectory() as td:
        victim = Path(td) / "target.txt"
        victim.write_text("needle here\n", encoding="utf-8")
        run([VENV_PY, str(CODE_SEARCH), "--outline", str(TARGET_FILE)])
        run([VENV_PY, str(DIFF_ENGINE), "--file", str(victim), "--patch", VALID_PATCH])
        run([VENV_PY, str(CONTROL), "--transport", "local", "init", "--goal", "g", "--next", "n"], cwd=Path(td))
    after = listening_ports()
    return {
        "benchmark": "zero-daemon verification",
        "listening_before": len(before),
        "listening_after": len(after),
        "new_listening_ports": sorted(after - before),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="write benchmarks/results.json")
    args = parser.parse_args()

    results = [
        bench_outline(),
        bench_diff(),
        bench_transport(),
        bench_ports(),
    ]

    if args.json:
        out = ROOT / "benchmarks" / "results.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"tool": "asha-harness", "results": results},
                                  indent=2), encoding="utf-8")
        print(f"wrote {out}")

    print("| Benchmark | Metric | Measured |")
    print("|---|---|---|")
    for r in results:
        name = r["benchmark"]
        for key, value in r.items():
            if key == "benchmark":
                continue
            print(f"| {name} | {key} | {value} |")


if __name__ == "__main__":
    main()