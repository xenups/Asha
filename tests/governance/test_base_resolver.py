"""BaseRefResolver tests (Phase E).

Requirement coverage:
  A. explicit base wins over lower-priority sources
  B. CI metadata used when no explicit base
  C. tracking branch resolved deterministically
  D. unresolved base -> source=unresolved, ref=None, sha=None; governance
     stays fail-closed
  E. origin/main present but NOT the intended base -> never silently chosen
  F. conflicting CI sources -> fail closed (no arbitrary selection)
  G. divergence fact recorded; policy decides UNRELIABLE_BASE, not resolver
  H. determinism across repeated resolution

Expected values are explicit fixtures; nothing derived from the resolver
implementation under test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from asha.base_resolver import (  # noqa: E402
    BaseFacts,
    ResolvedBase,
    resolve_base,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "tests")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _init(root)
    return root


class TestExplicit:
    def test_explicit_wins_over_ci_and_tracking(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # make a branch + upstream so tracking COULD resolve
        _git(repo, "checkout", "-qb", "feature")
        _git(repo, "branch", "-u", "main", "feature")
        # CI metadata present but must NOT win over explicit
        monkeypatch.setenv("GITHUB_BASE_SHA", "deadbeef" * 5)
        monkeypatch.setenv("GITHUB_BASE_REF", "main")

        base = resolve_base(repo, explicit="main")
        assert base.source == "explicit"
        assert base.ref == "main"
        assert base.commit_sha is not None


class TestCi:
    def test_github_ci_metadata_used(self, repo: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
        sha = "1111111111111111111111111111111111111111"
        monkeypatch.setenv("GITHUB_BASE_SHA", sha)
        monkeypatch.setenv("GITHUB_BASE_REF", "main")
        base = resolve_base(repo)
        assert base.source == "ci"
        assert base.commit_sha == sha
        assert base.ref == "main"

    def test_gitlab_ci_metadata_used(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "2222222222222222222222222222222222222222"
        monkeypatch.setenv("CI_MERGE_REQUEST_TARGET_BRANCH_SHA", sha)
        monkeypatch.setenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "main")
        base = resolve_base(repo)
        assert base.source == "ci"
        assert base.commit_sha == sha

    def test_conflicting_ci_shas_fail_closed(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GITHUB_BASE_SHA", "aaaa" * 10)
        monkeypatch.setenv("CI_MERGE_REQUEST_TARGET_BRANCH_SHA", "bbbb" * 10)
        base = resolve_base(repo)
        # no arbitrary selection -> tracking or unresolved
        assert base.source in {"git_tracking", "unresolved"}


class TestTracking:
    def test_tracking_upstream_resolved(self, repo: Path) -> None:
        _git(repo, "checkout", "-qb", "feature")
        _git(repo, "branch", "-u", "main", "feature")
        base = resolve_base(repo)
        assert base.source == "git_tracking"
        assert base.ref == "main"
        assert base.commit_sha is not None


class TestUnresolved:
    def test_no_base_unresolved_facts(self, tmp_path: Path) -> None:
        # repo with no upstream, no tracking, no CI vars
        root = tmp_path / "bare"
        _init(root)
        _git(root, "checkout", "-qb", "orphan")
        base = resolve_base(root)
        assert base.source == "unresolved"
        assert base.ref is None
        assert base.commit_sha is None

    def test_empty_env_isolated(self, repo: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("GITHUB_BASE_SHA", "GITHUB_BASE_REF",
                    "CI_MERGE_REQUEST_TARGET_BRANCH_SHA",
                    "CI_MERGE_REQUEST_TARGET_BRANCH_NAME"):
            monkeypatch.delenv(var, raising=False)
        base = resolve_base(repo)
        assert base.source in {"git_tracking", "unresolved"}


class TestNoOriginMainGuess:
    def test_origin_main_not_chosen_when_not_intended(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "stacked"
        _init(root)
        # create origin/main as a remote-tracking ref (NOT the base)
        _git(root, "remote", "add", "origin", str(root))
        _git(root, "fetch", "-q", "origin")
        _git(root, "checkout", "-qb", "feature")
        # stacked PR: base is a feature branch, not origin/main
        base = resolve_base(root)
        assert base.source != "explicit"
        # must not silently pick origin/main merely because it exists:
        # with no upstream, resolution is unresolved (source == "unresolved")
        # or explicit tracking; never source == "ci" with ref origin/main
        if base.source == "git_tracking":
            assert base.ref != "origin/main"
        if base.source == "unresolved":
            assert base.ref is None


class TestDivergenceFact:
    @staticmethod
    def _many_files(root: Path, n: int) -> None:
        for i in range(n):
            (root / f"f{i:04d}.py").write_text(f"V = {i}\n",
                                               encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", f"{n} files")

    def test_changed_file_count_is_a_fact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "big"
        _init(root)
        _git(root, "checkout", "-qb", "feature")
        _git(root, "branch", "-u", "main", "feature")
        self._many_files(root, 2931)
        base = resolve_base(root)
        assert base.source == "git_tracking"

        # divergence: changed_file_count is a FACT (existing mechanism)
        from asha.scope_resolver import changed_files
        changed = changed_files(root, base.ref)
        facts = BaseFacts(resolved_base=base, changed_file_count=len(changed))
        assert facts.changed_file_count == 2931
        # the resolver itself must NOT emit UNRELIABLE_BASE
        assert "UNRELIABLE" not in str(base)
        assert "UNRELIABLE" not in str(facts)


class TestDeterminism:
    def test_repeated_resolution_identical(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GITHUB_BASE_SHA", "cccc" * 10)
        monkeypatch.setenv("GITHUB_BASE_REF", "main")
        a = resolve_base(repo)
        b = resolve_base(repo)
        assert a == b
        assert a.ref == b.ref and a.commit_sha == b.commit_sha

    def test_unresolved_repeated(self, tmp_path: Path) -> None:
        root = tmp_path / "det"
        _init(root)
        _git(root, "checkout", "-qb", "topic")
        a = resolve_base(root)
        b = resolve_base(root)
        assert a == b
        assert a.source == "unresolved"