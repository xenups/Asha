"""Phase 11.1 -- order-free unified CLI + snapshot-safe ``in_place_atomic``.

Covers the two friction points found in the Detailist PR #222 run:

* ``asha PATH --exec CMD --base REF`` used to fail parsing because the
  mode flag had to be ``argv[0]``; every natural permutation must now
  parse identically (Fix 1).
* ``in_place_atomic`` refused a dirty tip (``TIP_DIRTY``) and rolled
  back with ``git reset --hard``, destroying uncommitted WIP; the inner
  TDD loop must be able to call Asha and keep its work (Fix 2).

Fixtures are scratch git repos; the tool handlers and CLI entry points
run in-process (no MCP subprocess), same shape as test_mcp_phase11.py.
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
    """LF-only fixture writes (Windows ``write_text`` CRLF trap)."""
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
    ``@diff`` anchor (dev) resolves to a non-empty scope."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "dev")
    _git(repo, "checkout", "-q", "-b", "feat")
    _write(repo / "pkg/feature.py", "def feature() -> int:\n    return 7\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat work")
    return repo


def _dirty_wip(repo: Path, failing: bool = False) -> dict[str, str]:
    """Uncommitted WIP in the working tree: an edited tracked file plus a
    new untracked test module (a failing one by default)."""
    edited = ('VALUE = "base-wip"\n'
              "\n"
              "def base_helper(x: int) -> int:\n"
              "    return x + 1\n")
    _write(repo / "pkg/base.py", edited)
    body = ("def test_wip():\n    assert False\n" if failing
            else "def test_wip():\n    assert True\n")
    _write(repo / "tests/test_wip.py", body)
    return {"pkg/base.py": edited, "tests/test_wip.py": body}


def _status_set(repo: Path) -> set[str]:
    return {line for line in _git(repo, "status", "--porcelain").splitlines()
            if line.strip()}


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert isinstance(result, dict) and result.get("content"), result
    payload = json.loads(result["content"][0]["text"])
    assert isinstance(payload, dict)
    return payload


def _cli(argv: list[str], monkeypatch: Any, capsys: Any,
         repo: Path) -> tuple[int, dict[str, Any]]:
    monkeypatch.chdir(repo)
    code = cli.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


# --------------------------------------------------------------- Fix 1: CLI

def test_cli_exec_flag_after_path_parses(tmp_path: Path, monkeypatch: Any,
                                         capsys: Any) -> None:
    """The exact Detailist friction: PATH first, --exec later."""
    repo = _feature_repo(tmp_path)
    code, payload = _cli([str(repo), "--exec", PY, "-c", "print('go')",
                          "--base", "dev", "--json"], monkeypatch, capsys, repo)
    assert code == 0, payload
    assert payload["state"] == "DONE", payload
    assert payload["execution_mode"] == "in_place_atomic"


def test_cli_global_flags_in_any_order_are_equivalent(
        tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """Permutations of the same invocation must agree deterministically."""
    repo = _feature_repo(tmp_path)
    permutations = [
        [str(repo), "--exec", PY, "-c", "print('go')", "--base", "dev",
         "--json"],
        ["--base", "dev", str(repo), "--exec", PY, "-c", "print('go')",
         "--json"],
        ["--json", str(repo), "--base", "dev", "--exec", PY, "-c",
         "print('go')"],
    ]
    seen: list[tuple[int, str, str]] = []
    for argv in permutations:
        code, payload = _cli(argv, monkeypatch, capsys, repo)
        seen.append((code, payload["state"], payload["execution_mode"]))
    assert len(set(seen)) == 1, seen
    assert seen[0] == (0, "DONE", "in_place_atomic"), seen


def test_cli_stack_and_context_flags_after_path(tmp_path: Path,
                                               monkeypatch: Any,
                                               capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    code, payload = _cli([str(repo), "--stack", "dev", "feat", "--base",
                          "main", "--json"], monkeypatch, capsys, repo)
    assert code in (0, 1), payload
    assert payload["stack_healthy"] in (True, False)
    assert [entry["branch"] for entry in payload["branches"]] == \
        ["dev", "feat"], payload
    code, payload = _cli([str(repo), "--context", "pkg/base.py:base_helper",
                          "--json"], monkeypatch, capsys, repo)
    assert code == 0, payload


def test_cli_double_dash_hands_rest_to_exec_verbatim(
        tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    code, payload = _cli([str(repo), "--base", "dev", "--json", "--exec",
                          "--", PY, "-c", "print('verbatim')"],
                         monkeypatch, capsys, repo)
    assert code == 0, payload
    assert payload["state"] == "DONE", payload


def test_cli_conflicting_root_binding_exits_2(tmp_path: Path,
                                              monkeypatch: Any,
                                              capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    monkeypatch.chdir(repo)
    assert cli.main([str(repo), "--root", str(repo), "--json"]) == 2
    assert cli.main(["--bogus-flag", str(repo), "--json"]) == 2
    assert cli.main(["--exec", PY, "-c", "print('go')"]) == 0


# ------------------------------------------ Fix 2: dirty-WIP in_place runs

def test_in_place_dirty_wip_failing_cmd_preserves_wip(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    wip = _dirty_wip(repo, failing=True)
    before = _status_set(repo)
    result = server.asha_asha({"root": str(repo),
                               "cmd": [PY, "-m", "pytest", "-q",
                                       "tests/test_wip.py"]})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert payload["state"] == "FAILED", payload
    assert payload["validation_result"] == "FAIL", payload
    assert payload["ready_to_push"] is False, payload
    assert payload["rollback"]["restored"] is True, payload
    assert _status_set(repo) == before  # the WIP tree is byte-identical
    assert _read(repo / "pkg/base.py") == wip["pkg/base.py"]
    assert _read(repo / "tests/test_wip.py") == wip["tests/test_wip.py"]


def test_in_place_dirty_wip_failing_cmd_cli_exit_1(tmp_path: Path,
                                                  monkeypatch: Any,
                                                  capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    wip = _dirty_wip(repo, failing=True)
    code, payload = _cli([str(repo), "--exec", PY, "-m", "pytest", "-q",
                          "tests/test_wip.py", "--json"],
                         monkeypatch, capsys, repo)
    assert code == 1, payload
    assert payload["state"] == "FAILED", payload
    assert _read(repo / "tests/test_wip.py") == wip["tests/test_wip.py"]
    assert _read(repo / "pkg/base.py") == wip["pkg/base.py"]


def test_in_place_dirty_wip_out_of_scope_write_rolled_back(
        tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    wip = _dirty_wip(repo)
    before = _status_set(repo)
    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-c", "open('zz_outside.txt', 'w').write('x')"]})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert payload["state"] == "FAILED", payload
    assert "SCOPE_VIOLATION" in json.dumps(payload)
    assert not (repo / "zz_outside.txt").exists()
    assert _status_set(repo) == before
    assert _read(repo / "pkg/base.py") == wip["pkg/base.py"]


def test_in_place_dirty_wip_pass_seals_wip_evidence(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    wip = _dirty_wip(repo)
    before = _status_set(repo)
    result = server.asha_asha({"root": str(repo),
                               "cmd": [PY, "-m", "pytest", "-q",
                                       "tests/test_wip.py"],
                               "validation": [PY + " -m pytest -q "
                                              "tests/test_wip.py"]})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert payload["state"] == "DONE", payload
    assert payload["validation_result"] == "PASS", payload
    assert payload["ready_to_push"] is False, payload
    assert payload["reason"] == "WIP_UNCOMMITTED_PASS", payload
    assert payload["authorized_to_ship"] is False, payload
    assert payload["evidence"].get("evidence_sha256"), payload
    assert _status_set(repo) == before          # WIP survived the pass
    assert _read(repo / "pkg/base.py") == wip["pkg/base.py"]
    # commit the WIP -> sealing alone still does not validate anything, so
    # the zero-config facade reports NOT_CONFIGURED and withholds readiness
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip became real")
    clean = _payload(server.asha_asha({"root": str(repo)}))
    assert clean["ready_to_push"] is False, clean
    assert clean["reason"] == "VALIDATION_NOT_CONFIGURED", clean
    # a validated run on the clean tip is ready
    validated = _payload(server.asha_asha({
        "root": str(repo), "cmd": [PY, "-c", "pass"],
        "validation": [PY + " -c pass"]}))
    assert validated["ready_to_push"] is True, validated


def test_in_place_clean_tree_still_reports_ready_to_push(
        tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    result = server.asha_asha({"root": str(repo),
                               "cmd": [PY, "-c", "print('clean')"],
                               "validation": [PY + " -c pass"]})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert payload["state"] == "DONE", payload
    assert payload["validation_result"] == "PASS", payload
    assert payload["ready_to_push"] is True, payload
    assert payload["authorized_to_ship"] is False, payload


def test_in_place_dirty_conflict_still_fails_closed(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    _git(repo, "checkout", "-q", "dev")
    _write(repo / "pkg/base.py", 'VALUE = "dev side"\n')
    _git(repo, "commit", "-q", "-am", "dev side")
    _git(repo, "checkout", "-q", "main")
    _write(repo / "pkg/base.py", 'VALUE = "main side"\n')
    _git(repo, "commit", "-q", "-am", "main side")
    _git(repo, "checkout", "-q", "dev")
    proc = subprocess.run(["git", "merge", "main"], cwd=repo,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0  # unmerged index entries
    result = server.asha_asha({"root": str(repo),
                               "cmd": [PY, "-c", "print('nope')"]})
    assert result.get("isError"), result
    assert "TIP_CONFLICTED" in result["content"][0]["text"]


# ---------------------------------------------------- Fix 2: @diff auto-scope

def test_auto_scope_includes_working_tree_changes(tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    _dirty_wip(repo)
    resolved, _changed, error = server._resolve_auto(repo, ["@diff"])
    assert error is None, error
    assert resolved is not None, resolved
    assert "tests/test_wip.py" in resolved, resolved
    assert "pkg/base.py" in resolved, resolved
    assert "pkg/feature.py" in resolved, resolved  # committed branch diff
    # behavioural: a cmd touching ONLY the uncommitted file is in scope
    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-c",
                "open('tests/test_wip.py', 'a').write('\\n# touched\\n')"]})
    payload = _payload(result)
    assert not result.get("isError"), payload
    assert payload["state"] == "DONE", payload
    touched = _read(repo / "tests/test_wip.py")
    assert "# touched" in touched and "assert True" in touched


def test_auto_scope_rejects_a_newly_created_out_of_scope_file(
        tmp_path: Path) -> None:
    repo = _feature_repo(tmp_path)
    _dirty_wip(repo)
    result = server.asha_asha({
        "root": str(repo),
        "cmd": [PY, "-c", "open('brand_new.py', 'w').write('x')"]})
    payload = _payload(result)
    assert payload["state"] == "FAILED", payload
    assert "SCOPE_VIOLATION" in json.dumps(payload)
    assert not (repo / "brand_new.py").exists()


# ------------------------------------------------ Fix 1: asha-mcp one-shot

def test_asha_mcp_one_shot_argument_order_independence(
        tmp_path: Path, capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    first = server.main(["asha_verify_stack", str(repo),
                         "branches=dev,feat", "base_ref=main"])
    out_first = capsys.readouterr().out
    second = server.main(["asha_verify_stack", "branches=dev,feat",
                          "base_ref=main", str(repo)])
    out_second = capsys.readouterr().out
    assert first == second, (first, second)
    assert json.loads(out_first) == json.loads(out_second)


def test_asha_mcp_bare_path_still_routes_to_unified(tmp_path: Path,
                                                    capsys: Any) -> None:
    repo = _feature_repo(tmp_path)
    code = server.main([str(repo)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    # routing still works, but a zero-config read validates nothing and so
    # must not claim push readiness
    assert payload["ready_to_push"] is False, payload
    assert payload["reason"] == "VALIDATION_NOT_CONFIGURED", payload


if __name__ == "__main__":  # pragma: no cover - manual probe
    raise SystemExit(pytest.main([__file__, "-q"]))
