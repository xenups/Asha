"""Both governed entry points must agree: DAG and the MCP single-worker tool.

The MCP tool is a thin adapter over the same engine, so an inconclusive
acceptance must reach the caller with the same state, the same acceptance
record and the same absence of a seal on both paths. A green worker with an
acceptance command that produced no verdict is never DONE.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from asha.governance.dag import run_workers_dag
from asha.mcp_server import asha_run_spec

PY = sys.executable


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fx"\n')
    (tmp_path / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.ruff_cache/\n.mypy_cache/\n")
    (tmp_path / "calc.py").write_text("def mul(a, b):\n    return a * b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from calc import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n")
    for args in (["git", "init", "-q", str(tmp_path)],
                 ["git", "-C", str(tmp_path), "add", "-A"]):
        subprocess.run(args, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "base"],
                   check=True, capture_output=True)
    return tmp_path


def _worker() -> dict:
    """Writes a benign file, then declares an acceptance that cannot verdict."""
    verify = (f'{PY} -c "print(\'ModuleNotFoundError: No module named '
              f'\\\'asha_probe_dep\\\'\'); raise SystemExit(2)"')
    return {"id": "w1", "deps": [], "declared_scope": ["calc.py"],
            "reads": ["calc.py"], "writes": ["calc.py"],
            "verify_command": verify,
            "cmd": [PY, "-c", ("import pathlib;"
                               "pathlib.Path('calc.py').write_text("
                               "'def mul(a,b):\\n    return a*b\\n')")]}


def test_mcp_and_dag_agree_on_an_inconclusive_acceptance(
        tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    worker = _worker()
    dag_report = run_workers_dag(repo, [dict(worker)], task_id="parity-1")
    dag_state = dag_report["states"]["w1"]

    tool = asha_run_spec({"root": str(repo), "apply": True,
                          "spec_content": {"workers": [worker]}})
    assert not tool.get("isError"), tool
    mcp = json.loads(tool["content"][0]["text"])

    assert dag_state["state"] == "FAILED"
    assert dag_state["reason"] == "acceptance_failed"
    assert dag_state["acceptance"]["outcome"] == "inconclusive"
    assert dag_state["acceptance"]["exit_code"] == 2
    assert dag_state["acceptance"]["diagnosis"]["module"] == "asha_probe_dep"

    assert mcp["status"] != "ok"
    mcp_state = mcp["states"]["w1"]
    assert mcp_state["state"] == dag_state["state"]
    assert mcp_state["acceptance"]["outcome"] == "inconclusive"
    assert mcp_state["acceptance"]["exit_code"] == 2
    assert not mcp.get("evidence", {}).get("w1")
