"""M3 alignment: baseline runs the CANDIDATE's decision on the base tree.

Real worker/DAG executions (no mocked check runner): every comparison
asserted here comes from two actual check passes over two distinct trees.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from asha import cli, evidence
from asha.governance.dag import run_workers_dag

APP_ONE = "VALUE = 1\n"
APP_TWO = "VALUE = 2\n"
TEST_BASE = (
    "from src.app import VALUE\n"
    "\n"
    "\n"
    "def test_ok():\n"
    "    assert VALUE >= 1\n"
    "\n"
    "\n"
    "def test_old():\n"
    "    assert VALUE == 0\n"
)
TEST_FIXED = TEST_BASE.replace("assert VALUE == 0", "assert VALUE >= 1")
TEST_WITH_NEW = TEST_BASE + (
    "\n"
    "\n"
    "def test_new():\n"
    "    assert VALUE == 0\n"
)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fx"\nversion = "0.0.0"\n', encoding="utf-8")
    (root / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.ruff_cache/\n.mypy_cache/\n",
        encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "app.py").write_text(APP_ONE, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(TEST_BASE, encoding="utf-8")
    for argv in (["git", "init", "-q", str(root)],
                 ["git", "-C", str(root), "add", "-A"],
                 ["git", "-C", str(root), "-c", "user.email=t@t",
                  "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(argv, check=True, capture_output=True)
    return root


def _write_script(rel: str, text: str) -> str:
    return ("import pathlib;"
            f"pathlib.Path({rel!r}).write_text({text!r})")


def _worker(wid: str, script: str, deps: list[str] | None = None) -> dict:
    return {
        "id": wid,
        "deps": list(deps or []),
        "declared_scope": ["src/app.py", "tests/test_app.py"],
        "reads": ["src/app.py", "tests/test_app.py"],
        "writes": ["src/app.py", "tests/test_app.py"],
        "cmd": [sys.executable, "-c", script],
    }


def _run(root: Path, workers: list[dict], task: str,
         tmp_path: Path) -> dict[str, Any]:
    os.environ["ASHA_STATE_DIR"] = str(tmp_path / "state")
    return run_workers_dag(root, workers, task_id=task)


def _locs(items: Any) -> list[str]:
    return [item["location"] if isinstance(item, dict) else str(item)
            for item in items or []]


def _baseline_artifact(tmp_path: Path, wid: str) -> dict[str, Any]:
    hits = list((tmp_path / "state").rglob(f"{wid}.baseline.json"))
    assert hits, "baseline artifact missing"
    return json.loads(hits[0].read_text(encoding="utf-8"))


def test_valid_comparison_preexisting_only_is_sealed_and_done(
        tmp_path: Path) -> None:
    """Worker changes only src/app.py; test_old fails before AND after.

    Both passes must run the same check inventory on two distinct trees;
    the PRE_EXISTING_ONLY exemption is then authorized, sealed and DONE.
    """
    root = _repo(tmp_path)
    worker = _worker("w1", _write_script("src/app.py", APP_TWO))
    report = _run(root, [worker], "t-valid", tmp_path)

    st = report["states"]["w1"]
    cmp = st["comparison"]
    assert st["state"] == "DONE", st
    assert cmp["performed"] is True, cmp
    assert cmp["comparable"] is True, cmp
    assert cmp["reason"] == "COMPARED", cmp
    assert cmp["verdict"] == "PRE_EXISTING_ONLY", cmp
    assert cmp["baseline_tree"] != cmp["candidate_tree"], cmp
    assert st["delta_failures"] == [], st
    assert any("test_old" in item
               for item in _locs(st["baseline_failures"])), st

    ev_path = Path(report["evidence"]["w1"])
    payload = json.loads(ev_path.read_text(encoding="utf-8"))
    assert payload["comparison"] == cmp
    assert payload["verdict"] == "PRE_EXISTING_ONLY"
    assert payload["tree_hash"] == cmp["candidate_tree"]

    artifact = _baseline_artifact(tmp_path, "w1")
    assert artifact["execution"] == "ok", artifact
    baseline_names = [c["name"] for c in artifact["checks"]]
    candidate_names = [c["name"] for c in payload["checks"]]
    assert baseline_names and baseline_names == candidate_names, (
        baseline_names, candidate_names)


def test_delta_is_derived_from_two_real_runs(tmp_path: Path) -> None:
    """Candidate adds a failing test: baseline must NOT show it."""
    root = _repo(tmp_path)
    worker = _worker("w1", _write_script("tests/test_app.py", TEST_WITH_NEW))
    report = _run(root, [worker], "t-delta", tmp_path)

    st = report["states"]["w1"]
    assert st["state"] == "FAILED", st
    assert st["comparison"]["performed"] is True, st
    assert st["comparison"]["reason"] == "COMPARED", st
    assert st["comparison"]["verdict"] == "NEW_FAILURES", st
    assert [item for item in _locs(st["delta_failures"])
            if "test_new" in item], st
    assert not [item for item in _locs(st["baseline_failures"])
                if "test_new" in item], st
    assert [item for item in _locs(st["current_failures"])
            if "test_new" in item], st


def test_fixing_preexisting_failure_is_no_failures(tmp_path: Path) -> None:
    """Baseline red, candidate green: NO_FAILURES with an empty delta."""
    root = _repo(tmp_path)
    worker = _worker("w1", _write_script("tests/test_app.py", TEST_FIXED))
    report = _run(root, [worker], "t-fix", tmp_path)

    st = report["states"]["w1"]
    assert st["state"] == "DONE", st
    assert st["comparison"]["verdict"] == "NO_FAILURES", st
    assert st["comparison"]["performed"] is True, st
    assert st["current_failures"] == [], st
    assert st["delta_failures"] == [], st
    assert any("test_old" in item
               for item in _locs(st["baseline_failures"])), st
    assert report["evidence"].get("w1"), report


def test_new_file_only_change_has_no_baseline(tmp_path: Path) -> None:
    """A file absent from the base tree cannot be baselined: no verdict."""
    root = _repo(tmp_path)
    worker = _worker("w1", _write_script("src/brand_new.py", "X = 1\n"))
    worker["declared_scope"] = ["src/brand_new.py"]
    worker["writes"] = ["src/brand_new.py"]
    worker["reads"] = ["src/brand_new.py"]
    report = _run(root, [worker], "t-new", tmp_path)

    st = report["states"]["w1"]
    assert st["comparison"]["performed"] is False, st
    assert st["comparison"]["reason"] in ("NOT_COMPARED",
                                          "INVENTORY_MISMATCH"), st
    assert st["comparison"]["verdict"] is None, st


# ------------------------------------------------------- CLI agrees (D1)
def _sealed(tmp_path: Path, verdict: str | None,
            performed: bool) -> Path:
    record = evidence.seal({
        "schema": evidence.SCHEMA, "stage": "worker", "scope": "S0",
        "commit": "a" * 40, "tree_hash": "b" * 40,
        "observed_at": evidence.now_iso(),
        "checks": [{"name": "pytest", "status": "failed", "exit_code": 1,
                    "output_tail": "FAILED tests/t.py::test_old - assert 0"}],
        "authorized_to_ship": False, "target_tree_sha": "b" * 40,
        "verdict": verdict,
        "comparison": {"performed": performed, "baseline_tree": "a" * 40,
                       "candidate_tree": "b" * 40, "comparable": performed,
                       "reason": "COMPARED" if performed
                       else "BASE_EQUALS_TARGET",
                       "verdict": verdict if performed else None,
                       "detail": None},
        "acceptance": {"command": None, "supplied": False, "ran": False,
                       "exit_code": None, "outcome": "not_required"},
    })
    path = tmp_path / "sealed.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _cli_result(tmp_path: Path, path: Path) -> dict[str, Any]:
    sealed = json.loads(path.read_text(encoding="utf-8"))
    def _dispatch(root: Any, worker: dict, **kw: Any) -> dict:
        wid = worker["id"]
        return {
            "states": {wid: {"state": "DONE", "evidence": str(path),
                             "comparison": sealed["comparison"],
                             "verdict": sealed.get("verdict")}},
            "evidence": {wid: str(path)},
            "authoritative": {wid: b"record"},
        }

    # monkeypatching typed module attrs; setattr keeps mypy out of it
    for name, value in (
            ("governance_profile", lambda *a, **k: None),
            ("route", lambda *a, **k: type("R", (), {
                "mode": cli.RuntimeMode.FAST_PATH})()),
            ("run_worker_in_worktree", _dispatch),
            ("verify_worker_evidence", lambda *a, **k: True),
            ("parse_evidence", lambda raw: raw),
            ("verify_record", lambda parsed: type("R", (), {
                "verified": True})()),
            ("verify_bytes", lambda raw: type("R", (), {
                "verified": True})())):
        setattr(cli, name, value)
    return cli._run_sealed(tmp_path, ["tests/test_app.py"],
                           classification=None)


def test_cli_excuses_preexisting_only_when_comparison_performed(
        tmp_path: Path) -> None:
    ok = _cli_result(tmp_path, _sealed(tmp_path, "PRE_EXISTING_ONLY", True))
    assert ok["validation_result"] == "PASS", ok
    assert ok["evidence_id"], ok

    bad = _cli_result(tmp_path, _sealed(tmp_path, "PRE_EXISTING_ONLY", False))
    assert bad["validation_result"] == "FAIL", bad
