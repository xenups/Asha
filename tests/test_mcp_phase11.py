"""Phase 11 -- unified ``asha`` auto-routing facade (MCP) + one-command CLI.

Covers the two Phase 11 features end-to-end against scratch git repos
using the in-process tool handlers and the CLI entry points (no MCP
subprocess):

* ``asha`` MCP tool: zero-config status + sealed tip evidence on a
  clean repo, dirty-repo readiness report (no failure), ``target``
  single/batch surgical routing with ``TARGET_FORMAT_INVALID``,
  ``branches`` stack routing, ``cmd`` in-place atomic dispatch with
  ``@diff`` scope (pass + rollback), and strict precedence
  (cmd > branches > target > default).
* One-command CLI: ``asha PATH``, ``--verify``, ``--stack``,
  ``--context``, ``--exec``, plus ``asha-mcp /path`` bare-path
  one-shot routing. Existing CLI flows (schema-v1 evaluation,
  ``status``, unknown-tool exit 2) stay byte-identical.
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
    # a passing test so the default check suite (ruff/pytest/mypy) is
    # green when run_checks=True verification is exercised
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
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


def _feature_repo(tmp_path: Path) -> Path:
    """main -> dev -> feat with one committed change on feat: the bare
    ``@diff`` anchor (dev) resolves to a non-empty, existing scope."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "dev")
    _git(repo, "checkout", "-q", "-b", "feat")
    _write(repo / "pkg/feature.py", "def feature() -> int:\n    return 7\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat work")
    return repo


def _commit(repo: Path, rel: str, text: str, message: str) -> None:
    _write(repo / rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert isinstance(result, dict) and result.get("content"), result
    text = result["content"][0]["text"]
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


# ------------------------------------------------------------ registration

def test_asha_registered_first_and_eighth_tool() -> None:
    names = [spec["name"] for spec in server.TOOL_SPECS]
    assert names[0] == "asha", names
    # 10: Phase 3 appended asha_review / asha_review_status; registration
    # order for the pre-existing tools is unchanged.
    assert len(names) == 10, names
    assert names[-2:] == ["asha_review", "asha_review_status"], names
    assert "asha" in server.TOOL_HANDLERS


# ----------------------------------------------------------- zero-config

def test_unified_zero_config_clean_seals_evidence(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha({"root": str(repo)})
    payload = _payload(result)
    assert not result.get("isError"), payload
    # zero-config sealing records a snapshot but validates nothing
    assert payload["ok"] is False
    assert payload["authorized_to_ship"] is False
    assert payload["evidence_sha256"]
    assert payload["status"]["clean"] is True
    assert payload["ready_to_push"] is False
    assert payload["reason"] == "VALIDATION_NOT_CONFIGURED"
    assert payload["declared_scope"] == ["pkg/feature.py"]
    assert Path(payload["evidence_path"]).exists()


def test_unified_zero_config_dirty_reports_tip_dirty(
        tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    _write(repo / "pkg/dirty.py", "MARK = 1\n")
    result = server.asha_asha({"root": str(repo)})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert payload["ready_to_push"] is False
    assert payload["refusal"] == "TIP_DIRTY"
    assert any("pkg/dirty.py" in line for line in payload["status"])


def test_unified_zero_config_base_ref_plumbed(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha({"root": str(repo), "base_ref": "dev"})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert payload["base_ref"] == "dev"


# --------------------------------------------------------------- target

def test_unified_target_single_routes_surgical(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha(
        {"root": str(repo), "target": "pkg/base.py:base_helper"})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert "base_helper" in json.dumps(payload)


def test_unified_target_batch_routes_surgical(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha({"root": str(repo), "target": [
        "pkg/base.py:base_helper", "pkg/user.py:use_base"]})
    payload = _payload(result)
    assert not result.get("isError"), payload
    text = json.dumps(payload)
    assert "base_helper" in text and "use_base" in text


def test_unified_target_comma_separated_string(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha(
        {"root": str(repo), "target": "pkg/base.py:base_helper,"
                                      "pkg/user.py:use_base"})
    payload = _payload(result)
    assert not result.get("isError"), payload
    text = json.dumps(payload)
    assert "base_helper" in text and "use_base" in text


def test_unified_target_invalid_format_fails_closed(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha({"root": str(repo), "target": "no_colon.py"})
    assert result.get("isError"), result
    assert "TARGET_FORMAT_INVALID" in result["content"][0]["text"]
    result2 = server.asha_asha(
        {"root": str(repo), "target": "pkg/base.py:"})
    assert result2.get("isError"), result2
    assert "TARGET_FORMAT_INVALID" in result2["content"][0]["text"]


# ------------------------------------------------------------- branches

def test_unified_branches_routes_stack_verification(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha(
        {"root": str(repo), "branches": ["dev", "feat"], "base_ref": "main"})
    payload = _payload(result)
    assert not result.get("isError"), payload
    text = json.dumps(payload)
    assert "STACK_EVIDENCE_MISSING" in text   # no sealed tip evidence yet
    assert payload["stack_healthy"] is False


# ------------------------------------------------------------------ cmd

def test_unified_cmd_runs_in_place_atomic(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha({
        "root": str(repo), "cmd": [PY, "-c", "print('governed')"]})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert payload["state"] == "DONE", payload
    assert payload["execution_mode"] == "in_place_atomic"
    assert payload["authorized_to_ship"] is False


def test_unified_cmd_rollback_on_scope_violation(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-c", "open('zz_outside.txt', 'w').write('x')"]})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert payload["state"] == "FAILED", payload
    assert "SCOPE_VIOLATION" in json.dumps(payload)
    assert not (repo / "zz_outside.txt").exists()
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_unified_precedence_cmd_over_branches_and_target(
        tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha({
        "root": str(repo), "cmd": [PY, "-c", "print('first')"],
        "branches": ["dev", "feat"], "target": "pkg/base.py:base_helper"})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert payload["execution_mode"] == "in_place_atomic"


def test_unified_empty_cmd_fails_closed(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha({"root": str(repo), "cmd": []})
    assert result.get("isError"), result


# ------------------------------------------------------------------ CLI

def test_cli_bare_path_zero_config(tmp_path: Path,
                                   monkeypatch: Any,
                                   capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    monkeypatch.chdir(repo)
    code = cli.main([str(repo), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    # an unvalidated tip is a non-success exit (fail-closed CLI contract)
    assert code == 1
    assert payload["ok"] is False
    assert payload["ready_to_push"] is False
    assert payload["reason"] == "VALIDATION_NOT_CONFIGURED"


def test_cli_verify_flag(tmp_path: Path, monkeypatch: Any,
                         capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    monkeypatch.chdir(repo)
    code = cli.main(["--verify", str(repo), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["run_checks"] is True
    assert payload["authorized_to_ship"] is False


def test_cli_stack_flag(tmp_path: Path, monkeypatch: Any,
                        capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    monkeypatch.chdir(repo)
    code = cli.main(["--stack", "dev", "feat", "--base", "main", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1          # unhealthy stack = failed verification
    assert payload["stack_healthy"] is False
    assert "STACK_EVIDENCE_MISSING" in json.dumps(payload)


def test_cli_context_flag(tmp_path: Path, monkeypatch: Any,
                          capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    monkeypatch.chdir(repo)
    code = cli.main(["--context", "pkg/base.py:base_helper",
                     "pkg/user.py:use_base", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    text = json.dumps(payload)
    assert "base_helper" in text and "use_base" in text


def test_cli_exec_flag_rolls_back(tmp_path: Path, monkeypatch: Any,
                                  capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    monkeypatch.chdir(repo)
    code = cli.main(["--exec", PY, "-c",
                     "open('zz_outside.txt', 'w').write('x')", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 1
    assert payload["state"] == "FAILED"
    assert not (repo / "zz_outside.txt").exists()


def test_cli_exec_flag_success(tmp_path: Path, monkeypatch: Any,
                               capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    monkeypatch.chdir(repo)
    code = cli.main(["--exec", PY, "-c", "print('ok')", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["state"] == "DONE"
    assert payload["execution_mode"] == "in_place_atomic"


def test_cli_unknown_flag_is_usage_error(tmp_path: Path,
                                         monkeypatch: Any) -> None:
    repo = _feature_repo(tmp_path)
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--not-a-flag"])
    assert excinfo.value.code == 2


def test_cli_existing_flows_unchanged(tmp_path: Path, capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    code = cli.main(["--root", str(repo), "--json", "--no-execute"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["schema_version"] == 1
    # unified facade keys never leak into the schema-v1 contract
    assert "ready_to_push" not in payload
    assert set(payload) >= {"decision", "eligible", "execution_mode"}


def test_cli_status_subcommand_unchanged(tmp_path: Path,
                                         capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    code = cli.main(["status", str(repo)])
    out = capsys.readouterr().out
    assert code == 0
    assert "tip " in out


# ------------------------------------------------------- MCP one-shot

def test_mcp_oneshot_bare_path_routes_unified(tmp_path: Path,
                                              capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    code = server.main([str(repo)])
    out = capsys.readouterr().out
    assert code == 0
    assert "sealed" in out


def test_mcp_oneshot_asha_tool_with_positional_root(tmp_path: Path,
                                                    capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    code = server.main(["asha", str(repo)])
    out = capsys.readouterr().out
    assert code == 0
    assert "sealed" in out


def test_mcp_oneshot_unknown_tool_still_usage_error(capsys: Any) -> None:
    assert server.main(["definitely_not_a_tool"]) == 2
