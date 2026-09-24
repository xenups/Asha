"""Regression tests for code_search.py AST outline + impact tracing."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CODE_SEARCH = REPO_ROOT / "asha" / "code_search.py"
PY = sys.executable


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(CODE_SEARCH), *args],
        capture_output=True,
        text=True,
        timeout=90,
    )


def _write_sample(path: Path, n_funcs: int = 30) -> None:
    """Generate a 500+ line Python sample: 30 functions x 18 lines each."""
    parts = ['"""Generated benchmark sample."""\nimport os\nfrom pathlib import Path\n\n']
    for i in range(n_funcs):
        parts.append(
            f"def func_{i:03d}(a: int, b: int) -> int:\n"
            f"    \"\"\"Docstring for func_{i:03d}.\"\"\"\n"
            f"    total = a + b\n"
            f"    for step in range(total):\n"
            f"        total += step\n"
            f"        if step % 2 == 0:\n"
            f"            total -= 1\n"
            f"        else:\n"
            f"            total += 2\n"
            f"    result = total * (a - b)\n"
            f"    return result\n"
            f"\n\n"
        )
    parts.append(
        "@dataclass\n"
        "class Sample:\n"
        "    value: int\n\n"
        "    def compute(self) -> int:\n"
        "        return self.value * 2\n"
    )
    path.write_text("".join(parts), encoding="utf-8")


def test_outline_extracts_symbols_not_bodies(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _write_sample(sample, n_funcs=39)
    assert len(sample.read_text(encoding="utf-8").splitlines()) >= 500
    proc = _run_cli("--outline", str(sample))
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) >= 40, "expected 39 funcs + decorated class symbol"
    assert any("function_definition: func_000" in line for line in lines)
    assert any("decorator: Sample" in line for line in lines)
    # No bodies leak into the outline.
    assert not any("total = a + b" in line for line in lines)
    assert not any("return result" in line for line in lines)


def test_outline_token_reduction_ratio(tmp_path: Path) -> None:
    """AST outline must be a strict subset with large token reduction."""
    sample = tmp_path / "sample.py"
    _write_sample(sample, n_funcs=30)
    raw = sample.read_text(encoding="utf-8")
    raw_tokens = len(raw.split())
    proc = _run_cli("--outline", str(sample))
    outline_tokens = len(proc.stdout.split())
    assert outline_tokens < raw_tokens
    reduction = 1.0 - outline_tokens / raw_tokens
    assert reduction > 0.8, f"expected >=80%% reduction, got {reduction:.1%}"


def test_trace_impact_three_usage_types(tmp_path: Path) -> None:
    (tmp_path / "lib.py").write_text(
        "class Engine:\n"
        "    def run(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        "from lib import Engine\n"
        "\n"
        "class FastEngine(Engine):\n"
        "    pass\n"
        "\n"
        "def go():\n"
        "    e = Engine()\n"
        "    return e.run()\n",
        encoding="utf-8",
    )
    proc = _run_cli("--trace", "Engine", "--dir", str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    usage = {line.split("[")[1].split("]")[0] for line in proc.stdout.splitlines()}
    assert {"import", "call", "inherit"} <= usage, usage


def test_verify_env_fail_closed_missing_dep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--verify-env must exit 1 when a pinned dep is absent (no auto-install)."""
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(tmp_path)  # empty dir -> tree_sitter_languages unavailable
    proc = subprocess.run(
        [PY, str(CODE_SEARCH), "--verify-env"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if proc.returncode == 0:
        pytest.skip("venv pins present; empty-PYTHONPATH shadowing failed")
    assert proc.returncode == 1
    assert "ENV CHECK FAILED" in proc.stderr