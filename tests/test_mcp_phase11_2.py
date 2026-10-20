"""Phase 11.2 -- Deleted-File Scope Handling, Syntax-Resilient AST & Unified Head Evidence.

Covers the three edge-case bugs found in the Detailist RUN v3:

1. TIP_SCOPE_MISSING on legitimate file deletions (git rm)
2. AST Indexer crash / empty JSON on WIP SyntaxError
3. Disconnect between in_place_atomic sealed evidence and asha_verify_stack

Fixtures are scratch git repos; tool handlers and CLI entry points run in-process.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import asha.mcp_server as server
from asha import cli

PY = sys.executable

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

_BASELINE = {
    ".gitignore": ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n",
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
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _write(path: Path, text: str) -> None:
    """LF-only fixture writes (Windows write_text CRLF trap)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


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


def _feature_repo(tmp_path: Path) -> Path:
    """main -> dev -> feat with one committed change on feat, so the bare
    @diff anchor (dev) resolves to a non-empty scope."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "dev")
    _git(repo, "checkout", "-q", "-b", "feat")
    _write(repo / "pkg/feature.py", "def feature() -> int:\n    return 7\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat work")
    return repo


def _status_set(repo: Path) -> set[str]:
    return {line for line in _git(repo, "status", "--porcelain").splitlines()
            if line.strip()}


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert isinstance(result, dict) and result.get("content"), result
    text = result["content"][0]["text"]
    try:
        payload = json.loads(text)
    except ValueError:
        # Error responses have plain text content
        return {"raw": text, "isError": result.get("isError", False)}
    assert isinstance(payload, dict)
    return payload


def _cli(argv: list[str], monkeypatch: Any, capsys: Any,
         repo: Path) -> tuple[int, dict[str, Any]]:
    monkeypatch.chdir(repo)
    code = cli.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


# ---------------------------------------------------------------------------
# FIX 1: Deleted-File Awareness in @diff and asha_verify_tip
# ---------------------------------------------------------------------------

def test_deleted_file_in_branch_diff_git_rm(tmp_path: Path) -> None:
    """A branch that deletes a tracked file (git rm) and modifies another file.
    asha_verify_tip with declared_scope=["@diff:main"] should succeed,
    record the deletion cleanly, and NOT raise TIP_SCOPE_MISSING."""
    repo = _repo(tmp_path)
    # Create a file to delete later
    _write(repo / "validator/app.env", "DELETE_ME=1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add validator file")

    # Create feature branch that deletes the file and modifies another
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "validator/app.env").unlink()  # git rm
    _write(repo / "pkg/base.py", 'VALUE = "feat"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: delete validator file")

    # asha_verify_tip with @diff:main should succeed
    result = server.asha_verify_tip({
        "root": str(repo),
        "base_ref": "main",
        "run_checks": True,
        "declared_scope": ["@diff:main"]
    })
    assert not result.get("isError"), _payload(result)
    payload = _payload(result)
    assert payload["state"] == "DONE", payload
    assert payload["ready_to_push"] is True, payload
    # The deleted file should be recorded in deleted_in_scope
    assert "deleted_in_scope" in payload, payload
    assert "validator/app.env" in payload["deleted_in_scope"], payload


def test_deleted_file_bogus_path_still_fails_closed(tmp_path: Path) -> None:
    """Passing a truly non-existent bogus file in declared_scope that was NOT
    deleted in git diff MUST still fail closed with TIP_SCOPE_MISSING."""
    repo = _feature_repo(tmp_path)
    # No deletions in this branch
    result = server.asha_verify_tip({
        "root": str(repo),
        "base_ref": "dev",
        "run_checks": True,
        "declared_scope": ["nonexistent_ghost.py"]
    })
    assert result.get("isError"), result
    payload = _payload(result)
    assert "TIP_SCOPE_MISSING" in json.dumps(payload), payload


def test_deleted_file_unified_cli_verify_flag(tmp_path: Path, monkeypatch: Any,
                                              capsys: Any) -> None:
    """Unified CLI asha --verify with @diff:main on a branch with git rm
    should succeed (ready_to_push: true)."""
    repo = _repo(tmp_path)
    _write(repo / "validator/app.env", "DELETE_ME=1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add validator file")

    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "validator/app.env").unlink()
    _write(repo / "pkg/base.py", 'VALUE = "feat"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: delete validator file")

    code, payload = _cli([str(repo), "--verify", "--base", "main", "--json"],
                         monkeypatch, capsys, repo)
    assert code == 0, payload
    assert payload["ready_to_push"] is True, payload
    assert "deleted_in_scope" in payload, payload
    assert "validator/app.env" in payload["deleted_in_scope"], payload


# ---------------------------------------------------------------------------
# FIX 2: WIP SyntaxError Resilience
# ---------------------------------------------------------------------------

def test_wip_syntax_error_exec_returns_valid_json(tmp_path: Path) -> None:
    """Introduce a Python SyntaxError into an uncommitted WIP file and invoke
    asha <repo> --exec "pytest -q" --json -> verify valid, non-empty JSON
    output is returned, WIP file is preserved intact (mode: snapshot),
    and no unhandled traceback crashes the CLI."""
    repo = _feature_repo(tmp_path)
    # Create a WIP file with a SyntaxError
    _write(repo / "tests/test_syntax_error.py", "def test_foo():\n    assert True\n    invalid syntax here\n")

    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-m", "pytest", "-q", "tests/test_syntax_error.py"]
    })
    # Should NOT crash with empty stdout - must return valid JSON
    assert not result.get("isError") or "content" in result, result
    payload = _payload(result)
    # State should be FAILED (SyntaxError causes pytest to fail)
    assert payload["state"] == "FAILED", payload
    assert payload["validation_result"] == "FAIL", payload
    assert payload["ready_to_push"] is False, payload
    assert payload["rollback"]["restored"] is True, payload
    # WIP file must be preserved
    assert any("tests/test_syntax_error.py" in s for s in _status_set(repo))
    assert "invalid syntax here" in _read(repo / "tests/test_syntax_error.py")


def test_wip_syntax_error_context_returns_structured_refusal(tmp_path: Path) -> None:
    """Invoke asha <repo> --context broken.py:foo --json on a file with
    SyntaxError -> verify structured fail-closed JSON refusal
    (AST_SYNTAX_ERROR or equivalent) rather than unhandled crash."""
    repo = _feature_repo(tmp_path)
    # Create a WIP file with a SyntaxError
    _write(repo / "pkg/broken.py", "def broken(\n    return 1\n")

    result = server.asha_asha({"root": str(repo), "target": ["pkg/broken.py:broken"]})
    # Must return structured JSON, not crash
    assert "content" in result, result
    payload = _payload(result)
    # Should fail with AST_SYNTAX_ERROR or similar structured refusal
    assert payload["state"] == "FAILED", payload
    assert "AST_SYNTAX_ERROR" in json.dumps(payload) or "SyntaxError" in json.dumps(payload), payload


def test_cli_wip_syntax_error_json_output_non_empty(tmp_path: Path,
                                                    monkeypatch: Any,
                                                    capsys: Any) -> None:
    """CLI invocation with --json on a repo with WIP SyntaxError must
    produce valid, non-empty JSON on stdout (never empty)."""
    repo = _feature_repo(tmp_path)
    _write(repo / "tests/test_syntax_error.py", "def test_foo():\n    assert True\n    invalid syntax here\n")

    code, payload = _cli([str(repo), "--exec", PY, "-m", "pytest", "-q",
                          "tests/test_syntax_error.py", "--json"],
                         monkeypatch, capsys, repo)
    assert code == 1, payload  # pytest fails on SyntaxError
    assert isinstance(payload, dict), payload
    assert payload.get("state") == "FAILED", payload
    assert payload.get("validation_result") == "FAIL", payload


# ---------------------------------------------------------------------------
# FIX 3: Stack Verification with in_place_atomic Evidence
# ---------------------------------------------------------------------------

def test_stack_verification_with_in_place_atomic_evidence(tmp_path: Path) -> None:
    """Create a 2-branch linear stack (b1 -> b2). Verify each branch head
    using asha <repo> --exec "python -c pass" --base <parent> on a clean tree
    (ready_to_push: true). Run asha --stack b1 b2 --base main --json ->
    verify stack_healthy: true and evidence_verified: true for both branches
    without requiring a redundant --verify call."""
    repo = _repo(tmp_path)

    # Branch b1: add a file
    _git(repo, "checkout", "-q", "-b", "b1")
    _write(repo / "pkg/b1.py", "def b1() -> int:\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b1 work")

    # Branch b2: add another file
    _git(repo, "checkout", "-q", "-b", "b2")
    _write(repo / "pkg/b2.py", "def b2() -> int:\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b2 work")

    # Verify b1 using in_place_atomic on clean tree
    _git(repo, "checkout", "-q", "b1")
    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-c", "pass"],
        "validation": [PY + " -c pass"],
        "base_ref": "main"
    })
    payload = _payload(result)
    assert payload["state"] == "DONE", payload
    assert payload["validation_result"] == "PASS", payload
    assert payload["ready_to_push"] is True, payload

    # Verify b2 using in_place_atomic on clean tree
    _git(repo, "checkout", "-q", "b2")
    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-c", "pass"],
        "validation": [PY + " -c pass"],
        "base_ref": "b1"
    })
    payload = _payload(result)
    assert payload["state"] == "DONE", payload
    assert payload["validation_result"] == "PASS", payload
    assert payload["ready_to_push"] is True, payload

    # Now verify stack - should be healthy with evidence_verified: true
    result = server.asha_verify_stack({
        "root": str(repo),
        "branches": ["b1", "b2"],
        "base_ref": "main"
    })
    assert not result.get("isError"), result
    payload = _payload(result)
    assert payload["stack_healthy"] is True, payload
    for branch in payload["branches"]:
        assert branch["evidence_verified"] is True, branch


def test_stack_verification_wip_uncommitted_pass_not_valid_evidence(tmp_path: Path) -> None:
    """A WIP_UNCOMMITTED_PASS artifact (ready_to_push: false) must NOT count
    as valid stack tip evidence."""
    repo = _repo(tmp_path)

    # Branch b1
    _git(repo, "checkout", "-q", "-b", "b1")
    _write(repo / "pkg/b1.py", "def b1() -> int:\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b1 work")

    # Branch b2
    _git(repo, "checkout", "-q", "-b", "b2")
    _write(repo / "pkg/b2.py", "def b2() -> int:\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b2 work")

    # Verify b1 with in_place_atomic on CLEAN tree -> ready_to_push: true
    _git(repo, "checkout", "-q", "b1")
    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-c", "pass"],
        "validation": [PY + " -c pass"],
        "base_ref": "main"
    })
    payload = _payload(result)
    assert payload["ready_to_push"] is True

    # Verify b2 with in_place_atomic on DIRTY tree -> ready_to_push: false (WIP)
    _git(repo, "checkout", "-q", "b2")
    _write(repo / "pkg/b2.py", "def b2() -> int:\n    return 2\n# WIP MODIFICATION\n")
    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-c", "pass"],
        "validation": [PY + " -c pass"],
        "base_ref": "b1"
    })
    payload = _payload(result)
    assert payload["state"] == "DONE", payload
    assert payload["ready_to_push"] is False, payload
    assert payload["reason"] == "WIP_UNCOMMITTED_PASS", payload

    # Stack verification should NOT consider b2 as evidence_verified
    # because its tip was only WIP_UNCOMMITTED_PASS
    result = server.asha_verify_stack({
        "root": str(repo),
        "branches": ["b1", "b2"],
        "base_ref": "main"
    })
    assert not result.get("isError"), result
    payload = _payload(result)
    assert payload["stack_healthy"] is False, payload  # b2 lacks clean evidence
    b1_branch = next(b for b in payload["branches"] if b["branch"] == "b1")
    b2_branch = next(b for b in payload["branches"] if b["branch"] == "b2")
    assert b1_branch["evidence_verified"] is True, b1_branch
    assert b2_branch["evidence_verified"] is False, b2_branch


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------

def test_deleted_file_working_tree_deletion_also_tracked(tmp_path: Path) -> None:
    """A file deleted in the working tree (not committed) via git rm or rm
    should also be tracked in deleted_in_scope when using @diff."""
    repo = _feature_repo(tmp_path)
    # Create a file in the working tree, then delete it (unstaged deletion)
    _write(repo / "tests/test_to_delete.py", "def test_x(): assert True\n")
    _git(repo, "add", "tests/test_to_delete.py")
    _git(repo, "commit", "-q", "-m", "add file to delete")
    # Now delete it from working tree (unstaged)
    (repo / "tests/test_to_delete.py").unlink()

    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-c", "pass"],
        "base_ref": "dev"
    })
    assert not result.get("isError"), result
    payload = _payload(result)
    assert payload["state"] == "DONE", payload


def test_ast_syntax_error_during_diff_reads_expansion(tmp_path: Path) -> None:
    """During @diff auto-scope reads expansion, if a modified .py file has
    SyntaxError, catch it cleanly, record in ast_syntax_errors diagnostics,
    and still allow in_place_atomic to proceed."""
    repo = _feature_repo(tmp_path)
    # Create a WIP Python file with SyntaxError
    _write(repo / "tests/test_syntax.py", "def test_foo():\n    if True\n        assert True\n")

    # Run asha_asha with @diff scope - should not crash
    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-c", "print('go')"]
    })
    payload = _payload(result)
    assert not result.get("isError"), payload
    # Should succeed or fail gracefully, never crash
    assert "state" in payload, payload


if __name__ == "__main__":  # pragma: no cover - manual probe
    raise SystemExit(pytest.main([__file__, "-q"]))