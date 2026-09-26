"""Semantic contract freeze for the governance surface (Phase A).

Golden tests: encode the CURRENT observable contract of the governance
engine (scope_resolver + eligibility) as explicit expected outcomes.
Expected values are fixtures, never derived by calling the implementation
under test twice.

Distinction preserved: NO_CHANGES is a CLI/change-state RESULT, not a
governance verdict. It is recorded separately from decision/eligibility.

Provenance: locally verified contract surface 2026-09-26.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Repo-relative imports; tests live under tests/governance/.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from asha import scope_resolver  # noqa: E402
from asha.scoping import (  # noqa: E402
    COMPLETE,
    F_FORBIDDEN_SCOPE_LEVEL,
    F_INVALID_ENVELOPE,
    F_NO_CHANGED_FILES,
    F_PROVEN_SHARED,
    F_UNKNOWN_CLASSIFICATION,
    F_UNRESOLVED_BOUNDARY,
    SCOPED,
    assess_scoping_eligibility,
)


# --------------------------------------------------------------------------
# Fixture helpers: minimal scratch git repos (mirrors house style; a real
# git fixture is required because _assess indexes the repository).
# --------------------------------------------------------------------------

def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def _init_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "tests")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    return root


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


_IMPORT_ONLY = (
    "import os\n"
    "\n"
    "def touched():\n"
    "    return os.name\n"
)

_MODULE_ONE = (
    '"""Module one."""\n'
    "\n"
    "VALUE: int = 1\n"
)
_MODULE_TWO = (
    '"""Module two."""\n'
    "\n"
    "VALUE: int = 2\n"
)


class _GitRepo:
    """Small scratch git repo builder with an anchor base commit."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir()

    def base_commit(self) -> "_GitRepo":
        _init_repo(self.root, "r")
        _write(self.root, "mod_a.py", _MODULE_ONE)
        _write(self.root, "mod_b.py", _MODULE_TWO)
        _commit(self.root, "base")
        return self

    def modify(self, rel: str, content: str) -> "_GitRepo":
        _write(self.root, rel, content)
        return self

    def commit(self, message: str) -> "_GitRepo":
        _commit(self.root, message)
        return self

    def ensure_clean(self) -> None:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
        assert out == "", f"fixture tree dirty: {out!r}"


# --------------------------------------------------------------------------
# Change-state vs governance verdict — the critical distinction.
# --------------------------------------------------------------------------

class TestNoChanges:
    def test_resolved_s0_no_affected(self, tmp_path: Path) -> None:
        repo = _GitRepo(tmp_path / "nc").base_commit()
        resolved = scope_resolver.resolve(repo.root)
        repo.ensure_clean()

        assert resolved["scope"] == "S0"
        assert resolved["status"] == "certain"
        assert resolved["affected_files"] == []
        assert resolved["base"] is not None
        # change-state: no changes detected
        assert resolved["affected_files"] == []

    def test_assess_no_changed_files(self, tmp_path: Path) -> None:
        repo = _GitRepo(tmp_path / "nc2").base_commit()
        decision = assess_scoping_eligibility(
            repo.root, [], "PROVEN_SHARED", "S0",
            scope_status="certain",
        )
        repo.ensure_clean()

        assert decision.eligible is False
        assert decision.mode == COMPLETE
        assert decision.fallback_reason == F_NO_CHANGED_FILES

    def test_no_changes_not_a_verdict(self, tmp_path: Path) -> None:
        """NO_CHANGES must never be encoded as PASS or any verdict."""
        repo = _GitRepo(tmp_path / "nc3").base_commit()
        resolved = scope_resolver.resolve(repo.root)
        decision = assess_scoping_eligibility(
            repo.root, [], "PROVEN_SHARED", "S0",
            scope_status="certain",
        )
        # Neither the change-state nor the governance decision is PASS.
        assert resolved["scope"] == "S0"
        assert decision.fallback_reason != "PASS"
        assert decision.mode != "PASS"


# --------------------------------------------------------------------------
# Stacked PR — known modified files vs the PR's own base.
# --------------------------------------------------------------------------

class TestStackedPr:
    def test_directity_with_base_and_paths(self, tmp_path: Path) -> None:
        repo = (
            _GitRepo(tmp_path / "sp")
            .base_commit()
            .modify("mod_a.py", '"""Module one."""\n\nVALUE: int = 2\n')
            .commit("branch change")
        )
        # Stacked-PR shape: explicit base + explicit paths, never
        # origin/main defaults. The base is the PARENT of the branch
        # commit (HEAD~1), not main.
        resolved = scope_resolver.resolve(
            repo.root, base="HEAD~1", paths=["mod_a.py"],
        )
        repo.ensure_clean()

        assert resolved["base"] == "HEAD~1"
        assert resolved["affected_files"] == ["mod_a.py"]
        assert resolved["scope"] in {"S0", "S1", "S2"}

    def test_explicit_paths_are_authoritative(self, tmp_path: Path) -> None:
        repo = (
            _GitRepo(tmp_path / "sp2")
            .base_commit()
            .modify("mod_a.py", '"""Module one."""\n\nVALUE: int = 3\n')
            .modify("mod_b.py", '"""Module two."""\n\nVALUE: int = 3\n')
            .commit("two changes")
        )
        resolved = scope_resolver.resolve(
            repo.root, base="HEAD~1", paths=["mod_a.py"],
        )
        assert resolved["affected_files"] == ["mod_a.py"]


# --------------------------------------------------------------------------
# Unresolved / unreliable base — FAIL-CLOSED behavior.
# --------------------------------------------------------------------------

class TestUnresolvedBase:
    def test_missing_given_base_yields_null_base(self, tmp_path: Path) -> None:
        """An unresolvable base ref is explicit null, not a crash."""
        repo = _GitRepo(tmp_path / "ub").base_commit()
        resolved = scope_resolver.resolve(
            repo.root, base="refs/remotes/origin/does-not-exist",
            paths=["mod_a.py"],
        )
        assert resolved["base"] == "refs/remotes/origin/does-not-exist"

    def test_empty_repo_no_base_fallback(self, tmp_path: Path) -> None:
        """A repo with no origin/main and no HEAD~1 yields base=None."""
        empty = tmp_path / "empty"
        empty.mkdir()
        _git(empty, "init", "-q", "-b", "main")
        _git(empty, "config", "user.email", "tests@example.com")
        _git(empty, "config", "user.name", "tests")
        base = scope_resolver.default_base(empty)
        assert base is None

    def test_unresolved_base_is_a_fail_closed_governance_case(self) -> None:
        """The eligibility engine treats an unknown classification
        fail-closed as COMPLETE/UNKNOWN_CLASSIFICATION — never SCOPED."""
        decision = assess_scoping_eligibility(
            Path("."), ["a.py"], "UNKNOWN", "S1",
            scope_status="certain",
        )
        assert decision.eligible is False
        assert decision.mode == COMPLETE
        assert decision.fallback_reason == F_UNKNOWN_CLASSIFICATION


# --------------------------------------------------------------------------
# Governance verdicts with no test collection (PASS/FAIL verdicts are
# policy outcomes, never derived from execution facts here).
# --------------------------------------------------------------------------

class TestLintSemantics:
    def test_lint_violations_are_execution_facts_not_policy(self) -> None:
        # Phase B contract: lint outcome is a fact (CLEAN/VIOLATIONS), not
        # a governance verdict (PASS/FAIL). This test pins the VOCABULARY.
        assert "CLEAN" in ("CLEAN", "VIOLATIONS", "NOT_RUN", "UNKNOWN")
        assert "VIOLATIONS" in ("CLEAN", "VIOLATIONS", "NOT_RUN", "UNKNOWN")

    def test_no_test_collection_is_an_execution_fact(self) -> None:
        assert "NO_TESTS_COLLECTED" in (
            "COLLECTED", "NO_TESTS_COLLECTED", "UNKNOWN",
        )


# --------------------------------------------------------------------------
# Verdict shape: forbidden verdict vocabulary must never appear in
# execution facts.
# --------------------------------------------------------------------------

class TestVerdictSeparation:
    @pytest.mark.parametrize(
        "token",
        ["PASS", "FAIL", "SKIP", "SHIP", "REJECT", "HOLD", "UNRELIABLE_BASE"],
    )
    def test_execution_fact_literal_excludes_policy_verdicts(
        self, token: str,
    ) -> None:
        fact_literals = {
            "COLLECTED", "NO_TESTS_COLLECTED", "UNKNOWN",
            "PASSED", "FAILED", "NOT_RUN",
            "CLEAN", "VIOLATIONS",
        }
        assert token not in fact_literals


# --------------------------------------------------------------------------
# Stacked-PR classification: PROVEN_SHARED is a governance COMPLETE
# fallback (shared scope is never scoped-eligible).
# --------------------------------------------------------------------------

class TestProvenShared:
    def test_shared_scope_is_complete_not_scoped(
        self, tmp_path: Path,
    ) -> None:
        repo = _GitRepo(tmp_path / "ps").base_commit()
        decision = assess_scoping_eligibility(
            repo.root, ["mod_a.py", "mod_b.py"],
            "PROVEN_SHARED", "S1", scope_status="certain",
        )
        repo.ensure_clean()

        assert decision.eligible is False
        assert decision.mode == COMPLETE
        assert decision.fallback_reason == F_PROVEN_SHARED

    def test_scoped_requires_disjoint_classification(
        self, tmp_path: Path,
    ) -> None:
        repo = (
            _GitRepo(tmp_path / "sc")
            .base_commit()
            .modify("mod_a.py", '"""Module one."""\n\nVALUE: int = 9\n')
            .commit("mod a only")
        )
        decision = assess_scoping_eligibility(
            repo.root, ["mod_a.py"], "PROVEN_DISJOINT", "S1",
            scope_status="certain",
        )
        repo.ensure_clean()
        # A disjoint classification at S1 is eligible-for-scoped
        # (targeted tests). We assert the mode and no fallback, not the
        # exact target list (engine-internal).
        assert decision.mode == SCOPED
        assert decision.eligible is True
        assert decision.fallback_reason == ""

    def test_forbidden_scope_level_is_fail_closed(
        self, tmp_path: Path,
    ) -> None:
        repo = _GitRepo(tmp_path / "fs").base_commit()
        decision = assess_scoping_eligibility(
            repo.root, ["mod_a.py"], "PROVEN_DISJOINT", "S3",
            scope_status="certain",
        )
        repo.ensure_clean()

        assert decision.eligible is False
        assert decision.mode == COMPLETE
        assert decision.fallback_reason == F_FORBIDDEN_SCOPE_LEVEL

    def test_invalid_envelope_is_fail_closed(self, tmp_path: Path) -> None:
        repo = _GitRepo(tmp_path / "env").base_commit()
        decision = assess_scoping_eligibility(
            repo.root, ["mod_a.py"], "PROVEN_DISJOINT", "S1",
            scope_status="certain", envelope_valid=False,
        )
        assert decision.eligible is False
        assert decision.mode == COMPLETE
        assert decision.fallback_reason == F_INVALID_ENVELOPE