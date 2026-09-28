"""Foreign-CWD standalone execution locks (J.4).

Every DIRECT_SCRIPT_SUPPORTED module must import and reach its CLI help
from a foreign working directory with an absolute script path -- the
contract the direct-script sys.path shims exist to provide. The
lowercase-filesystem-path failure mode (_asha_norm used to build the
inserted path) breaks exactly this scenario on case-sensitive hosts, so
these subprocess tests are the discriminating regression for it.

No optional dependencies are required: `--help` exits before any lazy
feature import (tree-sitter/ast-grep/mem0).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

MODULES = (
    "code_search.py",
    "diff_engine.py",
    "scope_resolver.py",
    "memory.py",
    "project_map.py",
)


def test_standalone_cli_help_from_foreign_cwd(tmp_path: Path) -> None:
    for name in MODULES:
        script = REPO_ROOT / "asha" / name
        proc = subprocess.run(
            [PY, str(script), "--help"],
            cwd=tmp_path,  # foreign cwd: repo root NOT on sys.path
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, f"{name}: rc={proc.returncode} stderr={proc.stderr}"
        assert "usage:" in proc.stdout.lower(), f"{name}: no usage output"


def test_scope_resolver_import_sensitive_standalone(tmp_path: Path) -> None:
    """Direct-script mode must resolve the function-level relative import
    ``from .base_resolver import resolve_base`` (default_base) with the
    exact-case root on sys.path and __package__ adopted -- the path the
    ``--help`` smoke cannot reach (Bug B regression lock, J.4)."""
    script = REPO_ROOT / "asha" / "scope_resolver.py"
    proc = subprocess.run(
        [PY, str(script), "--root", str(REPO_ROOT)],
        cwd=tmp_path,  # foreign cwd
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"rc={proc.returncode} stderr={proc.stderr}"
    )
    assert "scope=" in proc.stdout, f"no scope output: {proc.stdout[:200]}"