"""Regression: D.1 empty-diff guardrail.

An unresolved base (no explicit/CI/upstream, and no HEAD~1 available e.g.
single-commit or shallow clone) must NOT resolve to the vacuous
S0/pytest-only matrix when the diff is empty -- that would seal on
nothing (Golden Law: PASS(A)+PASS(B) != PASS(A union B)). The guard
elevates to full S2 (ruff+pytest+mypy), mirroring ship_gate's
restoration, so committed runtime files can never skip lint/type checks
silently.
"""

from __future__ import annotations

from pathlib import Path

from asha import scope_resolver


def _make_single_commit_repo(tmp_path: Path) -> Path:
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=root, check=True)
    return root


def test_unresolved_base_empty_diff_elevates_to_s2(tmp_path: Path) -> None:
    """Unresolved base + empty diff -> full S2 matrix, never S0."""
    root = _make_single_commit_repo(tmp_path)
    res = scope_resolver.resolve(root)
    assert res["base"] is None            # no explicit/CI/upstream/HEAD~1
    assert res["affected_files"] == []    # clean tree, no commits to diff
    assert res["scope"] == "S2", res["scope"]
    assert res["checks"] == ["ruff", "pytest", "mypy"], res["checks"]
    assert any("D.1" in r for r in res["reasons"]), res["reasons"]


def test_with_base_empty_diff_stays_s0(tmp_path: Path) -> None:
    """Empty diff WITH a resolvable base keeps the historical S0 matrix."""
    root = _make_single_commit_repo(tmp_path)
    import subprocess

    subprocess.run(["git", "commit", "-qm", "two", "--allow-empty"],
                   cwd=root, check=True)
    res = scope_resolver.resolve(root, base="HEAD~1")
    assert res["base"] == "HEAD~1"
    assert res["affected_files"] == []
    assert res["scope"] == "S0", res["scope"]
    assert res["checks"] == ["pytest"], res["checks"]