"""Acceptance-backed verify-tip sealing (multi-service monorepos).

The R4 Detailist run exposed three results that looked like one bug but are
three different contracts:

* ``--exec`` seals an EXECUTION attestation (``checks: []``) -- refused as
  ``VALIDATION_NOT_CONFIGURED``. That is correct and stays that way.
* ``--verify`` runs the default ruff/pytest/mypy matrix, which no
  multi-service repo can satisfy in one interpreter (each service has its own
  venv and its own lint debt).
* ``--stack`` only counts ``verify-tip`` seals, so a stack verified through
  any other path reads ``0/N sealed`` forever.

This module pins the fix: ``--verify --acceptance CMD`` treats the declared
command as THE required check, records its real exit code and pytest
pass/skip counts, seals a digest-bound verify-tip artifact bound to the exact
commit, and never converts a missing/failed/inconclusive acceptance into PASS.
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

_PASS_CMD = f"{PY} -m pytest tests -q"
_FAIL_CMD = f"{PY} -c 'import sys; sys.exit(7)'"
_EMPTY_CMD = f"{PY} -m pytest tests -q -k definitely_no_such_test"
_MISSING_CMD = "/nonexistent/asha-acceptance-probe"
# mutating probes for the TOCTOU gate: one tracked file, one untracked file
_MUTATE_TRACKED_PY = "from pathlib import Path; Path('svc_a/app.py').write_text('MUTATED')"
_MUTATE_TRACKED_CMD = f'{PY} -c "{_MUTATE_TRACKED_PY}"'
_MUTATE_UNTRACKED_PY = "from pathlib import Path; Path('probe_marker.txt').write_text('x')"
_MUTATE_UNTRACKED_CMD = f'{PY} -c "{_MUTATE_UNTRACKED_PY}"'


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for rel, text in files.items():
        _write(repo / rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _repo(tmp_path: Path) -> Path:
    """Multi-service fixture: two services, both changed by the stack."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _commit(repo, {
        ".gitignore": ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n",
        "svc_a/__init__.py": "",
        "svc_a/app.py": "VALUE = 1\n",
        "svc_b/__init__.py": "",
        "svc_b/app.py": "VALUE = 1\n",
        "tests/test_ok.py": "def test_ok():\n    assert True\n",
    }, "baseline")
    _git(repo, "branch", "dev")
    return repo


def _stack_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """dev -> s1 -> s2 -> s3, each link a committed multi-service change."""
    repo = _repo(tmp_path)
    branches: list[str] = []
    parent = "dev"
    for index in (1, 2, 3):
        branch = f"s{index}"
        _git(repo, "checkout", "-q", "-b", branch, parent)
        _commit(repo, {
            "svc_a/app.py": f"VALUE = {index + 1}\n",
            "svc_b/app.py": f"VALUE = {index + 1}\n",
        }, f"{branch} work")
        branches.append(branch)
        parent = branch
    return repo, branches


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert isinstance(result, dict) and result.get("content"), result
    return json.loads(result["content"][0]["text"])


def _seal_path(repo: Path, head: str | None = None) -> Path:
    resolved = repo.resolve()
    head = head or _git(resolved, "rev-parse", "HEAD").strip()
    tree = _git(resolved, "rev-parse", "HEAD^{tree}").strip()
    return (server.common_paths.get_verify_tip_dir(resolved)
            / f"{head[:12]}-{tree[:12]}.evidence.json")


def _evidence(repo: Path, head: str) -> dict[str, Any]:
    path = _seal_path(repo, head)
    assert path.is_file(), path
    return json.loads(path.read_text(encoding="utf-8"))


def _seal(repo: Path, base: str, command: str = _PASS_CMD) -> dict[str, Any]:
    return _payload(server.asha_verify_tip({
        "root": str(repo), "base_ref": base,
        "acceptance_command": command,
        "declared_scope": [f"@diff:{base}"]}))


# --------------------------------------------------------------- sealing

def test_acceptance_pass_seals_a_verification_bound_to_the_tip(
        tmp_path: Path) -> None:
    repo, branches = _stack_repo(tmp_path)
    _git(repo, "checkout", "-q", branches[-1])
    head = _git(repo, "rev-parse", "HEAD").strip()

    payload = _seal(repo, "s2")

    assert payload["state"] == "DONE", payload
    assert payload["validation_status"] == "PASS", payload
    assert payload["ok"] is True
    assert payload["ready_to_push"] is True
    assert payload["authorized_to_ship"] is False
    assert payload["acceptance"]["outcome"] == "passed"
    sealed = _evidence(repo, head)
    assert sealed["commit"] == head
    assert sealed["kind"] == "verification"
    assert sealed["validation_status"] == "PASS"
    assert sealed["authorized_to_ship"] is False
    assert sealed["acceptance"]["outcome"] == "passed"


def test_acceptance_failure_never_becomes_a_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat", "dev")
    _commit(repo, {"svc_a/app.py": "VALUE = 2\n"}, "feat work")
    head = _git(repo, "rev-parse", "HEAD").strip()

    result = server.asha_verify_tip({
        "root": str(repo), "base_ref": "dev",
        "acceptance_command": _FAIL_CMD})

    assert result["isError"] is True
    assert result["content"][0]["text"].startswith("TIP_VALIDATION_FAILED")
    sealed = _evidence(repo, head)
    assert sealed["validation_status"] == "FAIL"
    assert sealed["ok"] is False
    assert sealed["ready_to_push"] is False
    assert sealed["acceptance"]["exit_code"] == 7


def test_acceptance_that_collects_nothing_is_not_a_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat", "dev")
    _commit(repo, {"svc_a/app.py": "VALUE = 2\n"}, "feat work")
    head = _git(repo, "rev-parse", "HEAD").strip()

    result = server.asha_verify_tip({
        "root": str(repo), "base_ref": "dev",
        "acceptance_command": _EMPTY_CMD})

    assert result["isError"] is True
    sealed = _evidence(repo, head)
    assert sealed["validation_status"] in ("BLOCKED", "FAIL")
    assert sealed["validation_status"] != "PASS"
    assert sealed["acceptance"]["outcome"] == "inconclusive"


def test_acceptance_that_cannot_launch_is_not_a_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat", "dev")
    _commit(repo, {"svc_a/app.py": "VALUE = 2\n"}, "feat work")
    head = _git(repo, "rev-parse", "HEAD").strip()

    result = server.asha_verify_tip({
        "root": str(repo), "base_ref": "dev",
        "acceptance_command": _MISSING_CMD})

    assert result["isError"] is True
    sealed = _evidence(repo, head)
    assert sealed["validation_status"] != "PASS"
    assert sealed["ok"] is False
    assert sealed["acceptance"]["outcome"] == "not_executed"
    assert sealed["checks"][0]["launched"] is False


def test_execution_only_tip_remains_not_configured(tmp_path: Path) -> None:
    """No acceptance, no checks: an execution-capable tip is not validated."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat", "dev")
    _commit(repo, {"svc_a/app.py": "VALUE = 2\n"}, "feat work")

    payload = _payload(server.asha_verify_tip({
        "root": str(repo), "base_ref": "dev"}))

    assert payload["state"] == "DONE"
    assert payload["validation_status"] == "NOT_CONFIGURED"
    assert payload["ok"] is False
    assert payload["ready_to_push"] is False


# ----------------------------------------------------------------- stacks

def _stack(repo: Path, branches: list[str]) -> dict[str, Any]:
    return _payload(server.asha_verify_stack({
        "root": str(repo), "branches": branches, "base_ref": "dev"}))


def test_linear_but_unsealed_stack_stays_unhealthy(tmp_path: Path) -> None:
    repo, branches = _stack_repo(tmp_path)
    stack = _stack(repo, branches)
    assert stack["stack_healthy"] is False
    assert all(row["is_linear"] for row in stack["branches"])
    assert all(row["evidence"] == "missing" for row in stack["branches"])


def test_multi_service_stack_seals_when_every_tip_has_acceptance_evidence(
        tmp_path: Path) -> None:
    repo, branches = _stack_repo(tmp_path)
    parent = "dev"
    for branch in branches:
        _git(repo, "checkout", "-q", branch)
        sealed = _seal(repo, parent)
        assert sealed["validation_status"] == "PASS", sealed
        parent = branch

    stack = _stack(repo, branches)

    assert [row["evidence"] for row in stack["branches"]] == ["verified"] * 3
    assert stack["stack_healthy"] is True


def test_tampered_receipt_unseals_the_stack(tmp_path: Path) -> None:
    repo, branches = _stack_repo(tmp_path)
    parent = "dev"
    for branch in branches:
        _git(repo, "checkout", "-q", branch)
        assert _seal(repo, parent)["validation_status"] == "PASS"
        parent = branch
    assert _stack(repo, branches)["stack_healthy"] is True

    path = _seal_path(repo)
    sealed = json.loads(path.read_text(encoding="utf-8"))
    # content edit without recomputing the digest: the artifact is bound to
    # its bytes, so a rewrite must be named, never silently re-trusted.
    sealed["commit"] = "0" * 40
    path.write_text(json.dumps(sealed), encoding="utf-8")

    stack = _stack(repo, branches)

    assert stack["stack_healthy"] is False
    assert stack["branches"][-1]["evidence"] in ("tampered", "mismatch")


def test_receipt_is_bound_to_the_exact_commit(tmp_path: Path) -> None:
    repo, branches = _stack_repo(tmp_path)
    parent = "dev"
    for branch in branches:
        _git(repo, "checkout", "-q", branch)
        assert _seal(repo, parent)["validation_status"] == "PASS"
        parent = branch
    assert _stack(repo, branches)["stack_healthy"] is True

    _commit(repo, {"svc_a/app.py": "VALUE = 99\n"}, "unsealed follow-up")
    stack = _stack(repo, branches)

    assert stack["stack_healthy"] is False
    assert stack["branches"][-1]["evidence"] != "verified"


def test_worktree_seal_is_visible_to_the_main_checkout(tmp_path: Path) -> None:
    """A stacked workflow checks each branch out in its own worktree; the
    seal must land in the repository's shared state, or `--stack` from the
    main checkout reads "0/N sealed" for a fully verified stack."""
    repo, branches = _stack_repo(tmp_path)
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "--detach", str(worktree),
         branches[-1])

    assert _seal(worktree, branches[-2])["validation_status"] == "PASS"

    stack = _payload(server.asha_verify_stack({
        "root": str(repo), "branches": [branches[-1]],
        "base_ref": branches[-2]}))

    assert stack["branches"][0]["is_linear"] is True
    assert stack["branches"][0]["evidence"] == "verified", stack
    assert stack["stack_healthy"] is True


# -------------------------------------------------------------------- CLI

def test_cli_verify_acceptance_seals_and_prints_pass(tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat", "dev")
    _commit(repo, {"svc_a/app.py": "VALUE = 2\n"}, "feat work")

    code = cli.main(["--verify", str(repo), "--base", "dev",
                     "--acceptance", _PASS_CMD, "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["validation_status"] == "PASS"
    assert payload["authorized_to_ship"] is False


def test_cli_acceptance_is_rejected_for_exec(tmp_path: Path,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    _repo(tmp_path)

    code = cli.main(["--exec", PY, "-c", "print('ok')",
                     "--acceptance", _PASS_CMD, "--json"])

    out = capsys.readouterr().out
    assert code == 2, out
    assert "--acceptance is only valid with --verify" in out


def test_run_checks_with_acceptance_is_rejected(tmp_path: Path) -> None:
    """Two required checks would mislabel the policy, so the request dies
    before either path runs and nothing is sealed."""
    repo, _branches = _stack_repo(tmp_path)
    _git(repo, "checkout", "-q", "s2")
    head = _git(repo, "rev-parse", "HEAD").strip()

    result = server.asha_verify_tip({
        "root": str(repo), "base_ref": "dev", "run_checks": True,
        "acceptance_command": _PASS_CMD,
        "declared_scope": ["@diff:dev"]})

    assert result.get("isError") is True, result
    assert "not both" in result["content"][0]["text"]
    assert not _seal_path(repo, head).exists(), _seal_path(repo, head)


def test_run_checks_alone_keeps_the_matrix_policy(tmp_path: Path) -> None:
    """run_checks still runs ruff/pytest/mypy and seals the scope_matrix
    policy -- the acceptance path never leaks into it."""
    repo, _branches = _stack_repo(tmp_path)
    _git(repo, "checkout", "-q", "s2")
    head = _git(repo, "rev-parse", "HEAD").strip()

    payload = _payload(server.asha_verify_tip({
        "root": str(repo), "base_ref": "dev", "run_checks": True,
        "declared_scope": ["@diff:dev"]}))

    assert payload["acceptance"] is None
    assert [check["name"] for check in payload["checks"]] == [
        "ruff", "pytest", "mypy"]
    assert _evidence(repo, head)["validation"]["policy"] == "scope_matrix"


def test_acceptance_that_mutates_a_tracked_file_fails_closed(
        tmp_path: Path) -> None:
    """A check that edits the tree invalidates its own PASS."""
    repo, _branches = _stack_repo(tmp_path)
    _git(repo, "checkout", "-q", "s2")

    result = server.asha_verify_tip({
        "root": str(repo), "base_ref": "dev",
        "acceptance_command": _MUTATE_TRACKED_CMD})

    assert result.get("isError") is True, result
    assert "TIP_CHANGED_DURING_VALIDATION" in result["content"][0]["text"]
    assert not _seal_path(repo).exists(), _seal_path(repo)


def test_acceptance_that_creates_an_untracked_file_fails_closed(
        tmp_path: Path) -> None:
    """An untracked by-product leaves a tree no seal can speak for."""
    repo, _branches = _stack_repo(tmp_path)
    _git(repo, "checkout", "-q", "s2")

    result = server.asha_verify_tip({
        "root": str(repo), "base_ref": "dev",
        "acceptance_command": _MUTATE_UNTRACKED_CMD})

    assert result.get("isError") is True, result
    assert "TIP_CHANGED_DURING_VALIDATION" in result["content"][0]["text"]
    assert not _seal_path(repo).exists(), _seal_path(repo)


def test_post_validation_inspection_error_fails_closed(
        tmp_path: Path, monkeypatch) -> None:
    """An inspection error after the checks run must never reach a seal."""
    repo, _branches = _stack_repo(tmp_path)
    _git(repo, "checkout", "-q", "s2")
    real_inspect = server._inspect_tip
    calls = {"n": 0}

    def flaky_inspect(root, allow_dirty=False):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("inspection exploded")
        return real_inspect(root, allow_dirty)

    monkeypatch.setattr(server, "_inspect_tip", flaky_inspect)

    result = server.asha_verify_tip({
        "root": str(repo), "base_ref": "dev",
        "acceptance_command": _PASS_CMD})

    assert result.get("isError") is True, result
    assert "TIP_REINSPECTION_FAILED" in result["content"][0]["text"]
    assert not _seal_path(repo).exists(), _seal_path(repo)


def test_weaker_run_does_not_downgrade_a_sealed_pass(tmp_path: Path) -> None:
    """F1: a no-checks run keeps an existing PASS seal intact."""
    repo, _branches = _stack_repo(tmp_path)
    _git(repo, "checkout", "-q", "s2")
    first = server.asha_verify_tip({"root": str(repo), "base_ref": "dev",
                                    "acceptance_command": _PASS_CMD})
    assert not first.get("isError"), first
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert _evidence(repo, head)["kind"] == "verification"

    second = server.asha_verify_tip({"root": str(repo), "base_ref": "dev"})

    assert not second.get("isError"), second
    assert json.loads(second["content"][0]["text"])["evidence_written"] is False
    assert _evidence(repo, head)["kind"] == "verification"


def test_failed_run_after_a_pass_keeps_the_pass_seal(tmp_path: Path) -> None:
    """F1: a failing re-run reports failure but cannot erase the PASS."""
    repo, _branches = _stack_repo(tmp_path)
    _git(repo, "checkout", "-q", "s2")
    assert not server.asha_verify_tip({
        "root": str(repo), "base_ref": "dev",
        "acceptance_command": _PASS_CMD}).get("isError")

    failed = server.asha_verify_tip({"root": str(repo), "base_ref": "dev",
                                     "acceptance_command": _FAIL_CMD})

    assert failed.get("isError") is True, failed
    assert "TIP_VALIDATION_FAILED" in failed["content"][0]["text"]
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert _evidence(repo, head)["validation_status"] == "PASS"


def test_corrupt_seal_is_repaired_by_a_new_verification(tmp_path: Path) -> None:
    """F1: corruption is not 'stronger' -- it is replaced."""
    repo, _branches = _stack_repo(tmp_path)
    _git(repo, "checkout", "-q", "s2")
    path = _seal_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    result = server.asha_verify_tip({"root": str(repo), "base_ref": "dev",
                                     "acceptance_command": _PASS_CMD})

    assert not result.get("isError"), result
    assert json.loads(path.read_text(encoding="utf-8"))[
        "validation_status"] == "PASS"


def test_ignored_file_creation_is_outside_the_stability_check(
        tmp_path: Path) -> None:
    """F2 contract: ignored output is not part of the audited state."""
    repo, _branches = _stack_repo(tmp_path)
    _git(repo, "checkout", "-q", "s2")
    with open(repo / ".gitignore", "a", encoding="utf-8") as handle:
        handle.write("ignored_out/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore build output")
    command = (f"{PY} -c \"from pathlib import Path; "
               f"Path('ignored_out').mkdir(exist_ok=True); "
               f"Path('ignored_out/probe.txt').write_text('x')\"")

    result = server.asha_verify_tip({"root": str(repo), "base_ref": "dev",
                                     "acceptance_command": command})

    assert not result.get("isError"), result
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert _evidence(repo, head)["validation_status"] == "PASS"


def test_tip_evidence_state_requires_an_explicit_store(tmp_path: Path) -> None:
    """F4: no implicit evidence store."""
    with pytest.raises(TypeError):
        server._tip_evidence_state(  # type: ignore[call-arg]
            Path("/tmp"), "a" * 40, "b" * 40)
