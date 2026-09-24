"""Phase 5.6 TDD: asha_run_spec apply=true -- thin adapter over the SAME
governed engine control.py delegates to (GovernedScheduler.run()).

The MCP layer must add zero execution logic: worktrees, conflict gating,
scope verification, checks, and sealed evidence all come from the existing
asha. `authorized_to_ship` stays False -- exposing a run is not a
ship authority (Merge law, scheduler._collect).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import asha.mcp_server as server

PY = sys.executable


# -- fixtures (house style: real scratch git repo, copied from tests/test_integrator) ---

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True)
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(
        ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n")
    (repo / "README.md").write_text("# p56 fixture\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _write_cmd(target: str, body: str = "import os\n") -> list[str]:
    script = ("from pathlib import Path\n"
              f"Path({target!r}).write_text({body!r})\n")
    return [PY, "-c", script]


def _worker(wid: str, deps: list[str] | None = None,
            writes: list[str] | None = None,
            cmd: list[str] | None = None,
            declared_scope: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": wid,
        "deps": deps or [],
        "declared_scope": ["tests/"] if declared_scope is None
        else declared_scope,
        "reads": [],
        "writes": writes or [],
        "cmd": [PY, "-c", "pass"] if cmd is None else cmd,
    }


def _worktrees_dir(repo: Path) -> Path:
    return repo.parent / (repo.name + ".worktrees")


def _side_effects(repo: Path) -> tuple[bool, bool]:
    """(evidence tree exists, worktree sibling exists)."""
    return ((repo / ".jspace" / "cache" / "orchestrator").exists(),
            _worktrees_dir(repo).exists())


def _call(arguments: dict[str, Any],
          request_id: int = 60) -> dict[str, Any]:
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
         "params": {"name": "asha_run_spec", "arguments": arguments}})
    assert response is not None
    assert "result" in response, response
    result: dict[str, Any] = response["result"]
    if result["isError"]:
        return {"error": result["content"][0]["text"]}
    return json.loads(result["content"][0]["text"])


def _plan_generations_of(repo: Path,
                         workers: list[dict[str, Any]]) -> list[list[str]]:
    plan = server.asha_plan_dag({"root": str(repo), "workers": workers})
    assert plan["isError"] is False, plan
    return json.loads(plan["content"][0]["text"])["generations"]


# -- 1. apply=true executes a real worker ------------------------------------

def test_apply_true_executes_real_worker(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [_worker("w1", writes=["tests/a.py"],
                       cmd=_write_cmd("tests/a.py", "A = 1\n"))]
    payload = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    assert "error" not in payload, payload
    assert payload["dry_run"] is False
    assert payload["status"] == "ok"
    assert payload["states"]["w1"]["state"] == "DONE"
    assert payload["successful_workers"] == ["w1"]
    evidence_path = Path(payload["evidence"]["w1"])
    sealed = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert "tests/a.py" in sealed["diff"]


# -- 2. apply=false still performs zero execution (regression) ----------------

def test_apply_false_still_zero_execution(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    marker = tmp_path / "MARKER_RAN"
    workers = [_worker("w1", writes=["tests/a.py"],
                       cmd=_write_cmd("tests/a.py",
                                      f"open({str(marker)!r}, 'w').write('x')"))]
    payload = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": False})
    assert payload["dry_run"] is True
    assert not marker.exists()
    assert _side_effects(repo) == (False, False)


# -- 3/4/5. validation happens BEFORE any side effect -------------------------

def test_invalid_apply_type_fails_before_side_effects(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payload = _call({"spec_content": {"workers": [
        _worker("w1", writes=["tests/a.py"])], },
        "root": str(repo), "apply": "yes"})
    assert payload.get("error") == "apply must be a boolean"
    assert _side_effects(repo) == (False, False)


def test_invalid_spec_fails_before_side_effects(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [_worker("w1", writes=["tests/a.py"])]
    # both spec sources at once
    payload = _call({"spec_path": str(tmp_path / "x.json"),
                     "spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    assert "exactly one of spec_path or spec_content" in payload.get("error", "")
    assert _side_effects(repo) == (False, False)


def test_missing_cmd_fails_before_side_effects(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payload = _call({"spec_content": {"workers": [
        {"id": "bad", "deps": [], "declared_scope": ["tests/"],
         "reads": [], "writes": ["tests/a.py"]}]},
        "root": str(repo), "apply": True})
    assert "error" in payload
    assert "cmd" in payload["error"]
    assert _side_effects(repo) == (False, False)


# -- 6. worker non-zero exit -> FAILED (existing canonical reason) ------------

def test_worker_nonzero_exit_is_failed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [_worker("boom", writes=["tests/a.py"],
                       cmd=[PY, "-c", "import sys; sys.exit(3)"])]
    payload = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    assert payload["states"]["boom"]["state"] == "FAILED"
    assert payload["states"]["boom"]["reason"] == "worker_exit_3"
    assert payload["successful_workers"] == []
    assert payload["failed_workers"] == ["boom"]
    assert payload["status"] == "failed"


# -- 7. scope violation -> INVALID_EVIDENCE (canonical, from _collect) --------

def test_scope_violation_is_invalid_evidence(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # declared_scope covers tests/ only; the cmd writes OUTSIDE it.
    workers = [_worker("oops", writes=["root_oops.py"],
                       declared_scope=["tests/"],
                       cmd=_write_cmd("root_oops.py", "X = 1\n"))]
    payload = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    entry = payload["states"]["oops"]
    assert entry["state"] == "INVALID_EVIDENCE"
    assert entry["reason"].startswith("scope_violation:")
    assert "root_oops.py" in entry["reason"]
    assert payload["invalid_evidence_workers"] == ["oops"]


# -- 8. verification failure -> FAILED (check_runner, existing pipeline) ------

def test_verification_failure_is_failed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [_worker("badtest", writes=["tests/test_fail.py"],
                       cmd=_write_cmd(
                           "tests/test_fail.py",
                           "def test_fail():\n    assert False\n"))]
    payload = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    entry = payload["states"]["badtest"]
    assert entry["state"] == "FAILED"
    assert entry["reason"].startswith("verification_failed:")
    assert payload["status"] == "failed"


# -- 9. conflict gating preserved (ConflictManager defers, never bypassed) ----

def test_conflict_gating_preserved(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [
        _worker("A", writes=["tests/shared.py"],
                cmd=_write_cmd("tests/shared.py", "A = 1\n")),
        _worker("B", writes=["tests/shared.py"],
                cmd=_write_cmd("tests/shared.py", "B = 2\n")),
    ]
    payload = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    # the SAME worker ran its own manager: B was deferred against running A
    assert payload["deferrals"], payload.get("deferrals")
    assert payload["states"]["A"]["state"] == "DONE"
    assert payload["states"]["B"]["state"] == "DONE"


# -- 10. worktree isolation preserved ----------------------------------------

def test_worktree_isolation_preserved(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    cwd_script = ("from pathlib import Path; "
                  "Path('tests/cwd.py').write_text(str(Path.cwd()))")
    workers = [_worker("iso", writes=["tests/cwd.py"],
                       cmd=[PY, "-c", cwd_script])]
    payload = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    assert payload["states"]["iso"]["state"] == "DONE"
    sealed = json.loads(Path(payload["evidence"]["iso"]).read_text(
        encoding="utf-8"))
    # the recorded diff carries the worktree-local cwd file
    assert "tests/cwd.py" in sealed["diff"]
    # main tree never saw the worker's working directory contents
    assert not (repo / "tests" / "cwd.py").exists()
    assert _git(repo, "status", "--porcelain") == ""
    # cleanup follows the existing lifecycle (zero orphaned worktrees)
    assert payload["cleanup_errors"] == []
    assert not _worktrees_dir(repo).exists()


# -- 11. evidence produced for a successful apply -----------------------------

def test_evidence_produced_for_success(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [_worker("w1", writes=["tests/a.py"],
                       cmd=_write_cmd("tests/a.py", "A = 1\n"))]
    payload = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    sealed = json.loads(Path(payload["evidence"]["w1"]).read_text(
        encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", sealed["target_tree_sha"])
    assert sealed["observed_scope"] == ["tests/a.py"]
    assert sealed["exit_status"] == 0
    assert any(entry["status"] == "passed" for entry in sealed["checks"])
    assert re.fullmatch(r"[0-9a-f]{64}", sealed["evidence_sha256"])


# -- 12. authorized_to_ship stays false (Merge law) ---------------------------

def test_authorized_to_ship_stays_false(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [_worker("w1", writes=["tests/a.py"],
                       cmd=_write_cmd("tests/a.py", "A = 1\n"))]
    payload = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    assert payload["authorized_to_ship"] is False
    sealed = json.loads(Path(payload["evidence"]["w1"]).read_text(
        encoding="utf-8"))
    assert sealed["authorized_to_ship"] is False


# -- 13. multi-worker apply goes through the existing scheduler ---------------

def test_multi_worker_uses_existing_scheduler(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [
        _worker("W1", writes=["tests/one.py"],
                cmd=_write_cmd("tests/one.py", "ONE = 1\n")),
        _worker("W2", deps=["W1"], writes=["tests/two.py"],
                cmd=_write_cmd("tests/two.py", "TWO = 2\n")),
        _worker("W3", deps=["W2"], writes=["tests/three.py"],
                cmd=_write_cmd("tests/three.py", "THREE = 3\n")),
    ]
    payload = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    assert payload["status"] == "ok"
    assert payload["successful_workers"] == ["W1", "W2", "W3"]
    # the run report is GovernedScheduler's own vocabulary: a hand-rolled
    # sequential loop in the MCP layer cannot emit these fields.
    assert isinstance(payload["graph"], dict)
    assert "generation" in payload["graph"]
    assert "reconcile_passes" in payload["graph"]
    assert "stale_intents_dropped" in payload["graph"]
    assert payload["generation_count"] == 3
    assert payload["cleanup_errors"] == []


# -- 14. plan and apply share one normalization/topology ----------------------

def test_plan_and_apply_share_normalization(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [
        _worker("w1", writes=["tests/a.py"],
                cmd=_write_cmd("tests/a.py", "A = 1\n")),
        _worker("w2", deps=["w1"], writes=["tests/b.py"],
                cmd=_write_cmd("tests/b.py", "B = 2\n")),
    ]
    planned = _plan_generations_of(repo, workers)
    applied = _call({"spec_content": {"workers": workers},
                     "root": str(repo), "apply": True})
    assert applied["generations"] == planned
    assert applied["generation_count"] == len(planned) == 2
