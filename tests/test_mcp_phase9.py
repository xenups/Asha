"""Phase 9 -- real-world telemetry hardening regression suite.

Covers the three friction points surfaced by the Detailist stacked-PR
production run (/tmp/asha_detailist_run.log):

1. CLI ergonomics: ``asha-mcp <tool> [k=v ...] [PATH]`` one-shot mode and
   ``asha status PATH`` -- a bare positional path maps to ``root`` when
   ``root=``/``--root`` is not already given; ambiguity is a clear exit 2.
2. ``asha_status`` always carries a deterministic, non-empty
   ``response`` summary string across every repository state (clean,
   dirty, detached HEAD, rebase-in-progress, orphaned worktrees).
3. ``asha_verify_tip``: in-place, fail-closed verification of the
   current tip for stacked-PR / multi-branch rebase workflows -- no
   competing worktree, snapshot-bound sealed evidence, denial on
   dirty/conflicted/rebase-in-progress trees.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import asha.mcp_server as server
from asha import audit_snapshot, cli, evidence
from asha.common import paths as common_paths

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _write(path: Path, text: str) -> None:
    # LF always: Path.write_text on Windows would bake CRLF into the
    # fixture blobs and `git diff --check` would then flag every line.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.open("w", encoding="utf-8", newline="\n").write(text)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo / ".gitignore",
           ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n"
           ".ruff_cache/\n")
    _write(repo / "README.md", "# fixture\n")
    pkg = repo / "pkg"
    pkg.mkdir()
    _write(pkg / "__init__.py", "")
    _write(pkg / "mod.py", "VALUE = 1\n")
    tests = repo / "tests"
    tests.mkdir()
    _write(tests / "test_ok.py", "def test_ok():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments}})
    assert response is not None
    result = response["result"]
    text = result["content"][0]["text"]
    try:
        payload: Any = json.loads(text)
    except ValueError:
        payload = {"_text": text}
    return {"isError": result["isError"], "payload": payload, "text": text}


def _cli(argv: list[str], stdin: str = "") -> tuple[int, str, str]:
    """Run an entrypoint in-process; catches argparse SystemExit."""
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = server.main(argv) if argv is not None else 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


# -- 1. CLI one-shot ergonomics -------------------------------------------

def test_mcp_cli_positional_path(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    code, out, _ = _cli(["asha_status", str(repo)])
    assert code == 0
    payload = json.loads(out)          # stdout = the tool's text content
    assert payload["clean"] is True
    assert payload["response"]


def test_mcp_cli_root_kv_still_works(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    code, out, _ = _cli(["asha_status", f"root={repo}"])
    assert code == 0
    payload = json.loads(out)
    assert payload["clean"] is True
    assert payload["response"]


def test_mcp_cli_refusal_exits_1(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo / "pkg" / "mod.py", "VALUE = 9\n")
    code, out, _ = _cli(["asha_verify_tip", str(repo)])
    assert code == 1
    assert out.startswith("TIP_DIRTY")


def test_mcp_cli_ambiguous_root_exit_2(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    code, _, err = _cli(["asha_status", str(repo), f"root={repo}"])
    assert code == 2
    assert "ambiguous" in err


def test_mcp_cli_extra_positional_exit_2(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    code, _, err = _cli(["asha_status", str(repo), "extra-token"])
    assert code == 2
    assert "positional" in err


def test_mcp_cli_unknown_tool_exit_2() -> None:
    code, _, err = _cli(["no_such_tool", "root=."])
    assert code == 2
    assert "unknown tool" in err


def test_mcp_cli_without_args_serves_stdio() -> None:
    # zero args -> unchanged JSON-RPC stdio server (EOF -> clean exit 0)
    code, out, _ = _cli([])
    assert code == 0
    assert out == ""


def test_asha_status_positional_human(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    code = cli.main(["status", str(repo)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "clean" in printed


def test_asha_status_json_includes_response(tmp_path: Path,
                                            capsys) -> None:
    repo = _make_repo(tmp_path)
    code = cli.main(["status", "--json", str(repo)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["response"]


def test_asha_status_ambiguous_exit_2(tmp_path: Path, capsys) -> None:
    repo = _make_repo(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["status", "--root", str(repo), str(repo)])
    assert excinfo.value.code == 2
    assert "positional" in capsys.readouterr().err


# -- 2. deterministic non-empty `response` --------------------------------

@pytest.mark.parametrize("state", ["clean", "dirty", "detached",
                                   "rebase", "orphan"])
def test_status_response_nonempty_every_state(tmp_path: Path,
                                              state: str) -> None:
    repo = _make_repo(tmp_path)
    if state == "dirty":
        _write(repo / "pkg" / "mod.py", "VALUE = 2\n")
    elif state == "detached":
        _git(repo, "commit", "-q", "--allow-empty", "-m", "second")
        _git(repo, "checkout", "-q", "HEAD~1")
    elif state == "rebase":
        (repo / ".git" / "rebase-merge").mkdir(parents=True, exist_ok=True)
    elif state == "orphan":
        orphan = repo.parent / (repo.name + ".worktrees") / "stale-x"
        orphan.mkdir(parents=True, exist_ok=True)
    result = _call("asha_status", {"root": str(repo)})
    assert not result["isError"], result
    response = result["payload"]["response"]
    assert isinstance(response, str) and response.strip(), result


def test_status_response_deterministic(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    first = _call("asha_status", {"root": str(repo)})["payload"]["response"]
    second = _call("asha_status", {"root": str(repo)})["payload"]["response"]
    assert first == second


def test_status_keeps_existing_keys(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payload = _call("asha_status", {"root": str(repo)})["payload"]
    for key in ("root", "head", "clean", "status", "worktrees", "lock"):
        assert key in payload


# -- 3. asha_verify_tip ----------------------------------------------------

def _tip(repo: Path, **kwargs: Any) -> dict[str, Any]:
    return _call("asha_verify_tip", {"root": str(repo), **kwargs})


def _artifact_path(repo: Path, head: str) -> Path:
    return (common_paths.get_state_dir(repo) / "verify-tip"
            / f"{head[:12]}.evidence.json")


def test_verify_tip_clean_tip_seals_evidence(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    result = _tip(repo)
    assert not result["isError"], result
    payload = result["payload"]
    assert payload["ok"] is True
    assert payload["head"] == head
    assert payload["snapshot_id"] == audit_snapshot.snapshot_id_of(repo)
    assert _HEX64.match(payload["evidence_sha256"])
    assert payload["authorized_to_ship"] is False
    assert payload["response"].strip()
    artifact = Path(payload["evidence_path"])
    assert artifact == _artifact_path(repo, head)
    sealed = json.loads(artifact.read_text(encoding="utf-8"))
    assert sealed["evidence_sha256"] == evidence.compute_digest(sealed)
    assert sealed["authorized_to_ship"] is False
    assert sealed["commit"] == head


def test_verify_tip_dirty_refused_no_artifact(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    _write(repo / "pkg" / "mod.py", "VALUE = 3\n")
    result = _tip(repo)
    assert result["isError"], result
    assert result["text"].startswith("TIP_DIRTY")
    assert not _artifact_path(repo, head).exists()


def test_verify_tip_conflicted_refused(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo / "pkg" / "mod.py", "VALUE = 10\n")
    _git(repo, "commit", "-q", "-am", "side change")
    _git(repo, "checkout", "-q", "main")
    _write(repo / "pkg" / "mod.py", "VALUE = 20\n")
    _git(repo, "commit", "-q", "-am", "main change")
    subprocess.run(["git", "merge", "side"], cwd=repo, capture_output=True,
                   text=True, timeout=60)  # conflict is the fixture
    result = _tip(repo)
    assert result["isError"], result
    assert result["text"].startswith("TIP_CONFLICTED")


def test_verify_tip_rebase_in_progress_refused(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / ".git" / "rebase-merge").mkdir(parents=True, exist_ok=True)
    result = _tip(repo)
    assert result["isError"], result
    assert result["text"].startswith("TIP_REBASE_IN_PROGRESS")


def test_verify_tip_unknown_base_ref_refused(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = _tip(repo, base_ref="no-such-ref")
    assert result["isError"], result
    assert result["text"].startswith("TIP_BASE_UNKNOWN")


def test_verify_tip_missing_declared_scope_refused(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = _tip(repo, declared_scope=["ghost.py"])
    assert result["isError"], result
    assert result["text"].startswith("TIP_SCOPE_MISSING")


def test_verify_tip_tampered_artifact_breaks_digest(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").strip()
    result = _tip(repo)
    assert not result["isError"], result
    artifact = _artifact_path(repo, head)
    sealed = json.loads(artifact.read_text(encoding="utf-8"))
    sealed["authorized_to_ship"] = True          # tamper after sealing
    artifact.write_text(json.dumps(sealed), encoding="utf-8")
    recorded = sealed["evidence_sha256"]
    tampered = json.loads(artifact.read_text(encoding="utf-8"))
    assert evidence.compute_digest(tampered) != recorded


def test_verify_tip_invalid_root_fails_closed(tmp_path: Path) -> None:
    result = _call("asha_verify_tip", {"root": 42})
    assert result["isError"], result


def test_verify_tip_on_rebased_stack_tip(tmp_path: Path) -> None:
    """Simulated stacked-rebase flow: feature rebased onto a moved main,
    verified in place at the new clean tip (no worktree spawned), with
    declared-scope checks executed by the engine itself."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo / "pkg" / "mod.py", "VALUE = 42\n")
    _git(repo, "commit", "-q", "-am", "feature work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "main moves")
    _git(repo, "rebase", "main", "feature")
    _git(repo, "checkout", "-q", "feature")     # post-rebase clean tip
    worktrees_before = _git(repo, "worktree", "list").count("\n")
    result = _tip(repo, base_ref="main", run_checks=True,
                  declared_scope=["pkg/mod.py"])
    assert not result["isError"], result
    payload = result["payload"]
    assert payload["ok"] is True
    assert payload["base_ref"] == "main"
    names = {entry["name"] for entry in payload["checks"]}
    assert names, payload["checks"]
    assert all(entry["status"] in ("passed", "skipped")
               for entry in payload["checks"]), payload["checks"]
    assert _git(repo, "worktree", "list").count("\n") == worktrees_before
    sealed = json.loads(Path(payload["evidence_path"]).read_text(
        encoding="utf-8"))
    assert sealed["evidence_sha256"] == evidence.compute_digest(sealed)


def test_verify_tip_registered_in_tools_list() -> None:
    listing = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listing is not None
    names = [tool["name"] for tool in listing["result"]["tools"]]
    assert "asha_verify_tip" in names
