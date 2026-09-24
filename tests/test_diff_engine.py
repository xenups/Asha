"""Regression tests for diff_engine.py atomic SEARCH/REPLACE semantics."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DIFF_ENGINE = REPO_ROOT / "asha" / "diff_engine.py"
PY = sys.executable

VALID_PATCH = (
    "<<<<<<< SEARCH\n"
    "needle here\n"
    "=======\n"
    "replaced needle\n"
    ">>>>>>> REPLACE\n"
)


@pytest.fixture()
def victim(tmp_path: Path) -> Path:
    path = tmp_path / "target.txt"
    path.write_text("line one\nneedle here\nline three\n", encoding="utf-8")
    return path


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(DIFF_ENGINE), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_valid_patch_applies_atomically(victim: Path) -> None:
    proc = _run_cli("--file", str(victim), "--patch", VALID_PATCH)
    assert proc.returncode == 0, proc.stderr
    assert victim.read_text(encoding="utf-8") == (
        "line one\nreplaced needle\nline three\n"
    )
    # No temp siblings left behind.
    assert list(victim.parent.glob("*.tmp")) == []


def test_mismatched_patch_rejected_zero_corruption(victim: Path) -> None:
    before = victim.read_text(encoding="utf-8")
    bad = VALID_PATCH.replace("needle here", "absent line")
    proc = _run_cli("--file", str(victim), "--patch", bad)
    assert proc.returncode == 1, "mismatched SEARCH must exit 1"
    assert "SEARCH block not found" in proc.stderr
    assert victim.read_text(encoding="utf-8") == before, "file must be untouched"
    assert list(victim.parent.glob("*.tmp")) == []


def test_ambiguous_patch_rejected_zero_corruption(tmp_path: Path) -> None:
    path = tmp_path / "dup.txt"
    path.write_text("dup\ndup\nonce\n", encoding="utf-8")
    patch = (
        "<<<<<<< SEARCH\n"
        "dup\n"
        "=======\n"
        "x\n"
        ">>>>>>> REPLACE\n"
    )
    proc = _run_cli("--file", str(path), "--patch", patch)
    assert proc.returncode == 1
    assert "ambiguous" in proc.stderr
    assert path.read_text(encoding="utf-8") == "dup\ndup\nonce\n"


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    proc = _run_cli("--file", str(tmp_path / "nope.txt"), "--patch", VALID_PATCH)
    assert proc.returncode == 1
    assert "not found" in proc.stderr


def test_rollback_of_applied_patch(victim: Path) -> None:
    """The applied patch must be reversible by its own inverse hunk."""
    _run_cli("--file", str(victim), "--patch", VALID_PATCH)
    inverse = VALID_PATCH.replace("needle here", "replaced needle").replace(
        "replaced needle", "needle here", 1
    )
    # Inverse hunk: SEARCH = post-patch line, REPLACE = original line.
    inverse = (
        "<<<<<<< SEARCH\n"
        "replaced needle\n"
        "=======\n"
        "needle here\n"
        ">>>>>>> REPLACE\n"
    )
    proc = _run_cli("--file", str(victim), "--patch", inverse)
    assert proc.returncode == 0, proc.stderr
    assert victim.read_text(encoding="utf-8") == (
        "line one\nneedle here\nline three\n"
    )