"""BaseRefResolver — Git base-reference discovery (Phase E).

Boundary: DISCOVERS and RESOLVES a base candidate. It NEVER decides
whether that base is acceptable for governance (that is policy).

Resolution hierarchy (deterministic, no optimistic guessing):

    Tier 1: explicit API/CLI base
    Tier 2: CI metadata provider (provider-neutral environment extraction)
    Tier 3: git tracking/upstream discovery (verified candidate)
    Tier 4: UNRESOLVED_BASE (source="unresolved", ref=None, sha=None)

Facts vs policy:
  facts   : base_ref, base_commit_sha, base_source, changed_file_count
  policy  : UNRESOLVED_BASE / UNRELIABLE_BASE  (lives OUTSIDE this module)

No origin/main / main / master guessing. No AST/CodeGraph. No execution
tools beyond read-only git.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BaseSource = Literal["explicit", "ci", "git_tracking", "unresolved"]


@dataclass(frozen=True)
class ResolvedBase:
    """Factual base-resolution result. No governance verdicts."""

    ref: str | None
    commit_sha: str | None
    source: BaseSource


@dataclass(frozen=True)
class BaseFacts:
    """Factual base + divergence facts for the policy layer."""

    resolved_base: ResolvedBase
    changed_file_count: int | None = None


def _git(root: Path, *args: str) -> str | None:
    """Read-only git query; None on any failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _resolve_ref(root: Path, ref: str) -> str | None:
    """Resolve a ref to a commit SHA; None if unresolvable."""
    return _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")


def explicit_base(root: Path, ref: str) -> ResolvedBase | None:
    """Tier 1: an explicit API/CLI base wins; must be resolvable."""
    sha = _resolve_ref(root, ref)
    if sha is None:
        return None  # explicit but unresolvable -> no silent fallback
    return ResolvedBase(ref=ref, commit_sha=sha, source="explicit")


_CI_CANDIDATES = (
    ("GITHUB_BASE_SHA", "GITHUB_BASE_REF"),
    ("GITHUB_BASE_REF", "GITHUB_BASE_SHA"),
    ("CI_MERGE_REQUEST_TARGET_BRANCH_SHA", None),
    ("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", None),
    ("GITLAB_MERGE_REQUEST_TARGET_BRANCH_SHA", None),
)


def ci_base(root: Path) -> ResolvedBase | None:
    """Tier 2: provider-neutral CI metadata extraction.

    Deterministic only: if multiple independent CI vars provide
    CONFLICTING base SHAs, fail closed (None) rather than guessing.
    """
    shas: dict[str, str] = {}
    refs: dict[str, str] = {}
    for sha_var, ref_var in _CI_CANDIDATES:
        sha = os.environ.get(sha_var)
        if sha:
            shas[sha_var] = sha
        if ref_var:
            ref = os.environ.get(ref_var)
            if ref:
                refs[ref_var] = ref

    distinct_shas = set(shas.values())
    if len(distinct_shas) > 1:
        return None  # conflicting CI metadata -> fail closed
    if not shas:
        return None
    sha = next(iter(distinct_shas))
    # deterministic ref preference: base_ref var of the SAME provider
    # as the winning sha var (map sha var -> partner ref var)
    partner = {
        "GITHUB_BASE_SHA": "GITHUB_BASE_REF",
        "GITHUB_BASE_REF": "GITHUB_BASE_SHA",
        "CI_MERGE_REQUEST_TARGET_BRANCH_SHA":
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
    }
    ref = refs.get(partner.get(next(iter(shas)), ""), "") or None
    return ResolvedBase(ref=ref, commit_sha=sha, source="ci")


def git_tracking_base(root: Path) -> ResolvedBase | None:
    """Tier 3: verified git tracking/upstream candidate.

    Uses the branch's configured upstream (branch.<name>.merge + remote)
    when it exists and resolves. NEVER guesses origin/main.
    """
    branch = _git(root, "branch", "--show-current")
    if not branch:
        return None
    upstream = _git(root, "rev-parse", "--abbrev-ref",
                    "--symbolic-full-name", f"{branch}@{{upstream}}")
    if not upstream:
        return None
    sha = _resolve_ref(root, upstream)
    if sha is None:
        return None
    return ResolvedBase(ref=upstream, commit_sha=sha, source="git_tracking")


def resolve_base(root: Path, explicit: str | None = None) -> ResolvedBase:
    """Deterministic tiered resolution. NEVER guesses a default branch.

    Tier 1 explicit -> Tier 2 CI -> Tier 3 tracking -> unresolved.
    """
    root = Path(root).resolve()
    if explicit is not None:
        hit = explicit_base(root, explicit)
        if hit is not None:
            return hit
        return ResolvedBase(ref=None, commit_sha=None, source="unresolved")
    hit = ci_base(root)
    if hit is not None:
        return hit
    hit = git_tracking_base(root)
    if hit is not None:
        return hit
    return ResolvedBase(ref=None, commit_sha=None, source="unresolved")