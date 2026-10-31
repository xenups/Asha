"""Phase 10 -- Stack lineage, auto-diff scope, batch surgical context,
and atomic in-place dispatch.

Covers the four Phase 10 features end-to-end against scratch git repos
using the in-process tool handlers (no MCP subprocess):

* ``asha_verify_stack``: linear healthy stack, ancestry drift with a
  deterministic ``rebase --onto`` recipe, missing/tampered sealed tip
  evidence, and the STACK_EMPTY / BRANCH_NOT_FOUND / BASE_REF_NOT_FOUND
  refusals.
* ``"@diff"`` / ``"@diff:<base>"`` auto-scope: deterministic expansion,
  protected governance-path filtering, ``SCOPE_AUTO_EMPTY`` fail-closed.
* ``asha_get_surgical_context`` batch ``targets`` vs single backward
  compatibility.
* ``execution_mode="in_place_atomic"``: clean pass + sealed evidence,
  and fail-closed ``git reset --hard`` rollback on scope violation or
  command failure.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import asha.mcp_server as server
from asha import evidence
from asha.common import paths as common_paths

PY = sys.executable
_BASELINE = {
    ".gitignore": ".jspace/\n__pycache__/\n*.pyc\n",
    "README.md": "# fixture\n",
    "pkg/__init__.py": "",
    "pkg/base.py": ('VALUE = "base"\n'
                    "\n"
                    "def base_helper(x: int) -> int:\n"
                    "    return x + 1\n"),
    "pkg/user.py": ("from pkg.base import base_helper\n"
                    "\n"
                    "def use_base(y: int) -> int:\n"
                    "    return base_helper(y)\n"),
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _write(path: Path, text: str) -> None:
    """LF-only fixture writes (Windows ``write_text`` CRLF trap)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in _BASELINE.items():
        _write(repo / rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _commit(repo: Path, rel: str, text: str, message: str) -> None:
    _write(repo / rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).strip()


# ---------------------------------------------------------------- stack ---

def _seal_tip_for_head(repo: Path) -> str:
    """Seal READINESS evidence for the CURRENT HEAD (clean tree).

    A command-only run is an execution attestation, not validation evidence,
    so the seal carries an explicit validation command that ran and passed.
    """
    result = server.asha_asha({"root": str(repo),
                               "cmd": [PY, "-c", "pass"],
                               "validation": [PY + " -c pass"]})
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ready_to_push"] is True, payload
    return payload["head"]


def test_verify_stack_linear_healthy(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "s1")
    _commit(repo, "pkg/one.py", "ONE = 1\n", "s1")
    _git(repo, "checkout", "-q", "-b", "s2")
    _commit(repo, "pkg/two.py", "TWO = 2\n", "s2")
    _git(repo, "checkout", "-q", "-b", "s3")
    _commit(repo, "pkg/three.py", "THREE = 3\n", "s3")
    # sealed evidence for every tip, in stack order
    for branch in ("s1", "s2", "s3"):
        _git(repo, "checkout", "-q", branch)
        _seal_tip_for_head(repo)

    result = server.asha_verify_stack({
        "root": str(repo), "branches": ["s1", "s2", "s3"],
        "base_ref": "main"})
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["stack_healthy"] is True
    assert len(payload["branches"]) == 3
    for row in payload["branches"]:
        assert row["is_linear"] is True
        assert row["evidence"] == "verified"
        assert row["status"] is None
        assert row["rebase_recipe"] is None
    assert "healthy" in payload["response"]


def test_verify_stack_drift_recipe(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "s1")
    _commit(repo, "pkg/one.py", "ONE = 1\n", "s1")
    # sibling branch off main: s2's parent should be s1 -> drift
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "s2")
    _commit(repo, "pkg/other.py", "OTHER = 1\n", "s2")

    result = server.asha_verify_stack(
        {"root": str(repo), "branches": ["s1", "s2"], "base_ref": "main"})
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["stack_healthy"] is False
    s1, s2 = payload["branches"]
    assert s1["is_linear"] is True
    assert s2["is_linear"] is False
    assert s2["status"] == "STACK_ANCESTRY_DRIFT"
    expected = (f"git rebase --onto {_sha(repo, 's1')} "
                f"{_sha(repo, 'main')} s2")
    assert s2["rebase_recipe"] == expected
    assert "UNHEALTHY" in payload["response"]


def test_verify_stack_missing_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "s1")
    _commit(repo, "pkg/one.py", "ONE = 1\n", "s1")
    result = server.asha_verify_stack(
        {"root": str(repo), "branches": ["s1"], "base_ref": "main"})
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["stack_healthy"] is False
    row = payload["branches"][0]
    assert row["is_linear"] is True
    assert row["evidence"] == "missing"
    assert row["status"] == "STACK_EVIDENCE_MISSING"


def test_verify_stack_tampered_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "s1")
    _commit(repo, "pkg/one.py", "ONE = 1\n", "s1")
    head = _seal_tip_for_head(repo)
    tree = _git(repo, "rev-parse", f"{head}^{{tree}}").strip()
    artifact = (common_paths.get_verify_tip_dir(repo)
                / f"{head[:12]}-{tree[:12]}.evidence.json")
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["authorized_to_ship"] = True  # tamper: digest no longer matches
    artifact.write_text(json.dumps(payload), encoding="utf-8", newline="\n")

    result = server.asha_verify_stack(
        {"root": str(repo), "branches": ["s1"], "base_ref": "main"})
    assert not result.get("isError"), result
    report = json.loads(result["content"][0]["text"])
    row = report["branches"][0]
    assert row["evidence"] == "tampered"
    assert row["status"] == "STACK_EVIDENCE_TAMPERED"
    assert report["stack_healthy"] is False


def test_verify_stack_refusals(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    empty = server.asha_verify_stack(
        {"root": str(repo), "branches": [], "base_ref": "main"})
    assert empty.get("isError") is True
    assert "STACK_EMPTY" in empty["content"][0]["text"]

    no_base = server.asha_verify_stack(
        {"root": str(repo), "branches": ["s1"]})
    assert no_base.get("isError") is True
    assert "BASE_REF_NOT_FOUND" in no_base["content"][0]["text"]

    bad_base = server.asha_verify_stack(
        {"root": str(repo), "branches": ["s1"], "base_ref": "nope"})
    assert bad_base.get("isError") is True
    assert "BASE_REF_NOT_FOUND" in bad_base["content"][0]["text"]

    missing = server.asha_verify_stack(
        {"root": str(repo), "branches": ["nope"], "base_ref": "main"})
    assert missing.get("isError") is True
    assert "BRANCH_NOT_FOUND" in missing["content"][0]["text"]


def test_verify_stack_registered_as_seventh_tool(tmp_path: Path) -> None:
    assert "asha_verify_stack" in server.TOOL_HANDLERS
    names = [tool["name"] for tool in server.TOOL_SPECS]
    # Phase 11 added the unified `asha` facade as the #1 entrypoint;
    # the seven specialized tools (incl. verify_stack) stay intact.
    # 14: Phase 3 registers the host-driven Ladder lifecycle. Each tool
    # calls the same shared service; none of them starts a model call.
    assert len(names) == 14
    assert {"asha_review", "asha_review_context", "asha_review_submit_stage",
            "asha_review_next_stage", "asha_review_finalize",
            "asha_review_status"} <= set(names)
    assert names[0] == "asha"
    assert "asha_verify_stack" in names
    one_shot = subprocess.run(
        [PY, "-m", "asha.mcp_server", "asha_verify_stack",
         f"root={tmp_path}", "branches="],
        capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert one_shot.returncode == 1  # refusal exits 1, STACK_EMPTY inside
    assert "STACK_EMPTY" in one_shot.stdout


# ---------------------------------------------------------------- @diff ---

def test_auto_scope_expansion_and_protected_filter(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "work")
    _commit(repo, "pkg/extra.py", "EXTRA = 1\n", "code")
    _commit(repo, ".github/workflows/ci.yml", "name: ci\n", "ci")
    resolved, changed, error = server._resolve_auto(repo, ["@diff"])
    assert error is None
    assert resolved == ["pkg/extra.py"]  # .github/ never implicitly in
    assert changed == [".github/workflows/ci.yml", "pkg/extra.py"]

    with_explicit, _, error = server._resolve_auto(
        repo, ["@diff", "README.md"])
    assert error is None
    assert with_explicit == ["README.md", "pkg/extra.py"]


def test_auto_scope_empty_diff_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    resolved, changed, error = server._resolve_auto(repo, ["@diff"])
    assert resolved is None and changed == []
    assert error is not None and error.startswith("SCOPE_AUTO_EMPTY")

    bad_ref, _, error = server._resolve_auto(repo, ["@diff:nope"])
    assert bad_ref is None
    assert error is not None and error.startswith("SCOPE_AUTO_EMPTY")


def test_auto_scope_reads_expansion(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "work")
    _commit(repo, "pkg/user.py",
            "from pkg.base import base_helper\n\n"
            "def use_base(y: int) -> int:\n    return base_helper(y + 1)\n",
            "touch user")
    expanded = server._expand_reads(repo, ["pkg/user.py"], ["README.md"])
    # dependency closure of pkg/user.py reaches pkg/base.py (+ __init__)
    assert "pkg/base.py" in expanded
    assert "README.md" in expanded


def test_dispatch_auto_empty_refuses_before_execution(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)  # no changes vs main -> empty diff
    result = server.asha_dispatch_task({
        "id": "w1", "declared_scope": ["@diff"],
        "cmd": [PY, "-c", "print('never')"], "root": str(repo)})
    assert result.get("isError") is True
    assert "SCOPE_AUTO_EMPTY" in result["content"][0]["text"]


def test_verify_tip_auto_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "work")
    _commit(repo, "pkg/extra.py", "EXTRA = 1\n", "code")
    ok = server.asha_verify_tip(
        {"root": str(repo), "declared_scope": ["@diff"]})
    assert not ok.get("isError"), ok
    payload = json.loads(ok["content"][0]["text"])
    assert payload["declared_scope"] == ["pkg/extra.py"]

    empty_repo = _repo(tmp_path / "fresh")
    refused = server.asha_verify_tip(
        {"root": str(empty_repo), "declared_scope": ["@diff"]})
    assert refused.get("isError") is True
    assert "SCOPE_AUTO_EMPTY" in refused["content"][0]["text"]


# ---------------------------------------------------------------- batch ---

def test_surgical_batch_matches_single(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    single = server.asha_get_surgical_context({
        "target_file": "pkg/user.py", "target_symbol": "use_base",
        "repo_path": str(repo)})
    assert not single.get("isError"), single
    single_payload = json.loads(single["content"][0]["text"])

    batch = server.asha_get_surgical_context({
        "repo_path": str(repo),
        "targets": [{"target_file": "pkg/user.py",
                     "target_symbol": "use_base"},
                    {"target_file": "pkg/base.py",
                     "target_symbol": "base_helper"}]})
    assert not batch.get("isError"), batch
    payload = json.loads(batch["content"][0]["text"])
    assert payload["count"] == 2
    assert payload["results"][0] == single_payload
    assert payload["results"][1]["target"] == "pkg/base.py:base_helper"
    assert payload["results"][1]["context"]


def test_surgical_batch_validation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    both = server.asha_get_surgical_context({
        "target_file": "pkg/user.py", "target_symbol": "use_base",
        "repo_path": str(repo),
        "targets": [{"target_file": "pkg/base.py",
                     "target_symbol": "base_helper"}]})
    assert both.get("isError") is True  # single + batch = ambiguous

    empty = server.asha_get_surgical_context(
        {"repo_path": str(repo), "targets": []})
    assert empty.get("isError") is True

    bad = server.asha_get_surgical_context({
        "repo_path": str(repo),
        "targets": [{"target_file": "pkg/user.py",
                     "target_symbol": "use_base", "extra": "x"}]})
    assert bad.get("isError") is True

    no_target = server.asha_get_surgical_context({"repo_path": str(repo)})
    assert no_target.get("isError") is True


# ------------------------------------------------------- in-place atomic ---

def _dispatch_in_place(repo: Path, wid: str, cmd: list[str],
                       declared: list[str]) -> dict[str, Any]:
    return server.asha_dispatch_task({
        "id": wid, "declared_scope": declared, "cmd": cmd,
        "root": str(repo), "execution_mode": "in_place_atomic"})


def test_in_place_atomic_done_sealed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = _sha(repo, "HEAD")
    result = _dispatch_in_place(
        repo, "w1",
        [PY, "-c", "open('pkg/base.py', 'a').write('NEW = 1\\n')"],
        ["pkg/base.py"])
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["state"] == "DONE"
    assert payload["execution_mode"] == "in_place_atomic"
    assert payload["authorized_to_ship"] is False
    evidence_info = payload["evidence"]
    assert evidence_info.get("evidence_sha256")
    artifact = Path(evidence_info["path"])
    sealed = json.loads(artifact.read_text(encoding="utf-8"))
    assert evidence.compute_digest(sealed) == sealed["evidence_sha256"]
    assert sealed["authorized_to_ship"] is False
    assert sealed["stage"] == "dispatch_in_place_atomic"
    # change is present in the working tree, HEAD untouched (no commit)
    assert "NEW = 1" in (repo / "pkg/base.py").read_text(encoding="utf-8")
    assert _sha(repo, "HEAD") == before


def test_in_place_atomic_scope_violation_rollback(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    original = (repo / "pkg/user.py").read_text(encoding="utf-8")
    result = _dispatch_in_place(
        repo, "w2",
        [PY, "-c", "open('pkg/user.py', 'a').write('LEAK = 1\\n')"],
        ["pkg/base.py"])  # cmd writes OUTSIDE declared_scope
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["state"] == "FAILED"
    assert payload["reason"] == "SCOPE_VIOLATION"
    assert payload["rollback"]["reset"] is True
    # exact pre-execution state restored, zero residue
    assert (repo / "pkg/user.py").read_text(encoding="utf-8") == original
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_in_place_atomic_cmd_failure_rollback(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = _dispatch_in_place(
        repo, "w3",
        [PY, "-c",
         "open('pkg/base.py', 'a').write('BAD = 1\\n'); raise SystemExit(3)"],
        ["pkg/base.py"])
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["state"] == "FAILED"
    assert payload["reason"] == "CMD_FAILED"
    assert payload["exit_code"] == 3
    assert "BAD = 1" not in (repo / "pkg/base.py").read_text(
        encoding="utf-8")
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_in_place_atomic_dirty_wip_is_snapshotted(tmp_path: Path) -> None:
    """Phase 11.1 replaces the old TIP_DIRTY refusal: an uncommitted WIP
    tree is allowed, the run is bound to a pre-state snapshot, and the
    caller's WIP survives a failing command byte-for-byte."""
    repo = _repo(tmp_path)
    _write(repo / "pkg/dirty.py", "DIRTY = 1\n")  # untracked, no commit
    _write(repo / "pkg/base.py", "DIRTY_EDIT = 2\n")
    before = _git(repo, "status", "--porcelain")
    result = _dispatch_in_place(
        repo, "w4", [PY, "-c", "raise SystemExit(1)"], ["pkg/base.py"])
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["state"] == "FAILED", payload
    assert payload["rollback"]["mode"] == "snapshot", payload
    assert payload["rollback"]["restored"] is True, payload
    assert _git(repo, "status", "--porcelain") == before
    assert (repo / "pkg/base.py").read_text(encoding="utf-8") == \
        "DIRTY_EDIT = 2\n"


def test_in_place_atomic_conflict_still_refused(tmp_path: Path) -> None:
    """The dirty-tree relaxation never relaxes the conflict guard."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo / "pkg/base.py", "SIDE = 1\n")
    _git(repo, "commit", "-q", "-am", "side")
    _git(repo, "checkout", "-q", "main")
    _write(repo / "pkg/base.py", "MAIN = 1\n")
    _git(repo, "commit", "-q", "-am", "main")
    subprocess.run(["git", "merge", "side"], cwd=repo, capture_output=True,
                   text=True, timeout=60)
    result = _dispatch_in_place(
        repo, "w5", [PY, "-c", "print('never')"], ["pkg/base.py"])
    assert result.get("isError") is True
    assert "TIP_CONFLICTED" in result["content"][0]["text"]


def test_in_place_atomic_default_untouched(tmp_path: Path) -> None:
    """Omitting execution_mode keeps the exact legacy field contract."""
    spec = next(tool for tool in server.TOOL_SPECS
                if tool["name"] == "asha_dispatch_task")
    mode = spec["inputSchema"]["properties"]["execution_mode"]
    assert mode["default"] == "worktree"
    assert set(mode["enum"]) == {"worktree", "in_place_atomic"}
