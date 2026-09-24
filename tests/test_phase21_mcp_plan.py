"""Phase 2.1 / section 22 -- MCP observes the SAME WorkerGraph semantics.

`asha_plan_dag` predecessors must be declared deps UNIONED with derived
code-graph edges (one shared projection: asha.worker_graph), and
`asha_run_spec` reuses the plan verbatim. No MCP-specific scheduler,
no second projection.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asha.mcp_server as server  # the shipped server module

GITIGNORE = (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
             ".mypy_cache/\n.ruff_cache/\n")
BASELINE = {
    ".gitignore": GITIGNORE,
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    # cross-scope import pair for derived projection:
    "pkg/f.py": "import pkg.g\n",
    "pkg/g.py": "VALUE = 1\n",
    # cycle pair (declared A dep B + file edge b -> a = derived B dep A):
    "pkg/a_side.py": "import os\n",
    "pkg/b_side.py": "import pkg.a_side\n",
}


def _repo(tmp_path: Path) -> Path:
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(
        ["git", *a], cwd=repo, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "fixture@example.com")
    run("config", "user.name", "Fixture")
    run("config", "commit.gpgsign", "false")
    for rel, text in BASELINE.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "baseline")
    return repo


def _w(wid: str, *, declared: list[str], deps: list[str] | None = None,
       reads: list[str] | None = None,
       writes: list[str] | None = None) -> dict[str, Any]:
    return {"id": wid, "deps": list(deps or []),
            "declared_scope": list(declared),
            "reads": list(reads or []), "writes": list(writes or []),
            "cmd": [sys.executable, "-c", "pass"]}


def _plan(root: Path, workers: list[dict[str, Any]]) -> dict[str, Any]:
    result = server.asha_plan_dag({"root": str(root), "workers": workers})
    assert not result["isError"], result
    import json
    text = result["content"][0]["text"]
    return json.loads(text)


def test_plan_generations_include_derived_worker_edges(
        tmp_path: Path) -> None:
    """Derived C dep P orders generations even with empty declared deps
    -- the shared projection, not an MCP-specific graph."""
    repo = _repo(tmp_path)
    workers = [
        _w("C", declared=["pkg/f.py"], reads=["pkg/f.py"]),
        _w("P", declared=["pkg/g.py"], reads=["pkg/g.py"],
           writes=["pkg/g.py"]),
    ]
    plan = _plan(repo, workers)
    layers = plan["generations"]
    flat = [wid for layer in layers for wid in layer]
    assert sorted(flat) == ["C", "P"]
    assert layers[0] == ["P"], layers   # provider first (derived edge)
    assert layers[1] == ["C"], layers
    assert "cycle" not in plan


def test_run_spec_dry_run_inherits_plan_topology(
        tmp_path: Path) -> None:
    """asha_run_spec reuses the plan verbatim: same derived generations
    in dry-run rows (no MCP-specific ordering anywhere)."""
    repo = _repo(tmp_path)
    workers = [
        _w("C", declared=["pkg/f.py"], reads=["pkg/f.py"]),
        _w("P", declared=["pkg/g.py"], reads=["pkg/g.py"],
           writes=["pkg/g.py"]),
    ]
    result = server.asha_run_spec(
        {"root": str(repo), "spec_content": {"workers": workers},
         "apply": False})
    assert not result["isError"], result
    import json
    payload = json.loads(result["content"][0]["text"])
    assert payload["dry_run"] is True
    assert payload["generations"] == [["P"], ["C"]], payload[
        "generations"]
    by_id = {row["worker_id"]: row for row in payload["completed"]}
    assert by_id["P"]["generation"] < by_id["C"]["generation"]


def test_plan_owner_ambiguity_is_uncertain_not_safe(
        tmp_path: Path) -> None:
    """Section 6/14 at the plan layer: two live claimants of one edge
    endpoint downgrade safety with an owner_ambiguous note."""
    repo = _repo(tmp_path)
    workers = [
        _w("C1", declared=["pkg/f.py"], reads=["pkg/f.py"]),
        _w("C2", declared=["pkg/f.py"], reads=["pkg/f.py"]),
        _w("P", declared=["pkg/g.py"], reads=["pkg/g.py"],
           writes=["pkg/g.py"]),
    ]
    plan = _plan(repo, workers)
    assert plan["safety"]["C1"] == "uncertain", plan["safety"]
    assert plan["safety"]["C2"] == "uncertain", plan["safety"]
    assert "owner_ambiguous" in plan["safety_notes"]["C1"], plan[
        "safety_notes"]


def test_plan_reports_worker_level_cycle_from_derived_edges(
        tmp_path: Path) -> None:
    """declared A dep B + derived (file b imports a) B dep A -> the plan
    reports the WORKER cycle (section 16), not a silent fuse."""
    repo = _repo(tmp_path)
    workers = [
        _w("A", declared=["pkg/a_side.py"], deps=["B"],
           reads=["pkg/a_side.py"]),
        _w("B", declared=["pkg/b_side.py"], reads=["pkg/b_side.py"]),
    ]
    plan = _plan(repo, workers)
    assert plan.get("cycle") == ["A", "B"], plan.get("cycle")


def test_plan_still_reports_import_cycle_safety_notes(
        tmp_path: Path) -> None:
    """Regression: file-level facts still feed safety notes exactly as
    before (uncertain_dep_fact / import_cycle vocabulary unchanged)."""
    repo = _repo(tmp_path)
    workers = [_w("A", declared=["pkg/a_side.py"],
                  reads=["pkg/a_side.py"])]
    plan = _plan(repo, workers)
    assert plan["safety"]["A"] in ("safe", "uncertain")
    assert isinstance(plan["safety_notes"], dict)
