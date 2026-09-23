"""Tests for the Phase 2 immutable GraphState and online reconciliation.

TDD: graph_state.py / dep_index.py do not exist when this file is first
run -- collection must fail with ModuleNotFoundError first (demonstrated
pre-implementation failure), then go green after implementation.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[1] / ".hermes" / "tools"

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import dep_index  # (TOOLS must be on sys.path before these imports)
import graph_state
import orchestrator

# -- fixtures (house style: real scratch git repo) --------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(
        ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n")
    (repo / "README.md").write_text("# fixture\n")
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


def _worker(wid: str) -> dict[str, Any]:
    return {
        "id": wid, "deps": [], "declared_scope": ["tests/"],
        "reads": [], "writes": [],
        "cmd": [sys.executable, "-c", "pass"],
    }


# -- GraphState --------------------------------------------------------------

def test_empty_state_generation_zero() -> None:
    state = graph_state.GraphState.empty()
    assert state.generation == 0
    assert dict(state.nodes) == {}
    assert dict(state.edges) == {}
    assert dict(state.reverse_edges) == {}


def test_graph_state_is_immutable() -> None:
    state = graph_state.build_state(0, {"a.py": {"os"}})
    # Cast through Any so mypy lets the mutation through while the real
    # MappingProxyType still raises at runtime (the raise IS the test).
    nodes: Any = state.nodes
    with pytest.raises(TypeError):
        nodes["b.py"] = "file"
    edges: Any = state.edges
    with pytest.raises(TypeError):
        edges["x"] = frozenset()
    # Inner collections are immutable as well.
    assert isinstance(state.edges["a.py"], frozenset)
    assert state.reverse_edges["os"] == frozenset({"a.py"})


def test_stale_generation_intent_rejected() -> None:
    state0 = graph_state.GraphState.empty()
    intent = graph_state.DispatchIntent("W", state0.generation)
    assert intent.matches(state0)
    upgraded = graph_state.build_state(state0.generation + 1, {})
    # Generation upgrade (another worker changed topology): stale intent
    # must be discarded, never dispatched.
    assert not intent.matches(upgraded)
    fresh = graph_state.DispatchIntent("W", upgraded.generation)
    assert fresh.matches(upgraded)


# -- reconciliation ----------------------------------------------------------

def test_reconcile_adds_edges_and_bumps_generation() -> None:
    index = dep_index.DependencyIndex()
    state = graph_state.GraphState.empty()
    out = graph_state.reconcile(state, index,
                                {"pkg/a.py": "import os\n"})
    assert out.ok and out.reason is None
    assert out.state is not state
    assert out.generation == 1 == out.state.generation
    assert out.state.edges["pkg/a.py"] == frozenset({"os"})
    assert out.state.reverse_edges["os"] == frozenset({"pkg/a.py"})
    assert "pkg/a.py" in out.state.nodes
    assert out.affected == ("pkg/a.py",)
    assert out.added == 1 and out.removed == 0


def test_reconcile_delta_removes_stale_edge() -> None:
    index = dep_index.DependencyIndex()
    out = graph_state.reconcile(
        graph_state.GraphState.empty(), index,
        {"a.py": "import os\n"})
    assert out.ok
    out2 = graph_state.reconcile(out.state, index,
                                 {"a.py": "import sys\n"})
    assert out2.ok
    assert out2.generation == 2
    assert out2.added == 1 and out2.removed == 1
    assert out2.state.edges["a.py"] == frozenset({"sys"})
    assert "os" not in out2.state.reverse_edges


def test_unchanged_file_never_reparsed() -> None:
    index = dep_index.DependencyIndex()
    first = graph_state.reconcile(
        graph_state.GraphState.empty(), index,
        {"a.py": "import os\n"})
    assert index.parse_count == 1
    # Same path + same content in a later batch: cache hit, no parse,
    # but the batch is still a normal reconciliation pass (+1).
    second = graph_state.reconcile(first.state, index,
                                   {"a.py": "import os\n"})
    assert second.ok
    assert index.parse_count == 1
    assert second.generation == 2
    assert second.added == 0 and second.removed == 0


def test_cycle_fails_closed_retains_previous_state() -> None:
    index = dep_index.DependencyIndex()
    state = graph_state.GraphState.empty()
    updates = {
        "pkg/a.py": "import pkg.b\n",
        "pkg/b.py": "import pkg.a\n",
    }
    out = graph_state.reconcile(state, index, updates)
    assert not out.ok
    assert out.reason is not None and out.reason.startswith("cycle")
    # Previous GraphState retained BY REFERENCE; no partial publish.
    assert out.state is state
    assert out.generation == 0 and out.state.generation == 0
    assert dict(out.state.edges) == {}


def test_uncertain_fact_fails_closed() -> None:
    index = dep_index.DependencyIndex()
    state = graph_state.GraphState.empty()
    out = graph_state.reconcile(
        state, index,
        {"x.py": "importlib.import_module('m')\n"})
    assert not out.ok
    assert out.reason is not None and out.reason.startswith("uncertain")
    assert out.state is state
    assert out.state.generation == 0
    assert dict(out.state.edges) == {}


def test_coalesced_batch_bumps_generation_once() -> None:
    index = dep_index.DependencyIndex()
    state = graph_state.GraphState.empty()
    updates = {f"w{i}.py": "import os\n" for i in range(4)}
    out = graph_state.reconcile(state, index, updates)
    assert out.ok
    # One coalesced batch => generation increments by EXACTLY 1.
    assert out.generation == state.generation + 1
    assert set(out.files) == set(updates)
    assert all(f"w{i}.py" in out.state.nodes for i in range(4))


# -- integration: multi-worker coalescing through the real scheduler ---------

def test_multi_worker_completion_reconciles_batch(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    barrier = threading.Barrier(4)

    def execute(worker: dict[str, Any], worktree: Path) -> int:
        wid = str(worker["id"])
        (worktree / "tests" / f"w_{wid}.py").write_text(
            f"import os\nMARK = {wid!r}\n")
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:
            return 1
        return 0

    workers = [_worker(wid) for wid in "ABCD"]
    sched = orchestrator.GovernedScheduler(
        repo, workers, task_id="p2-coalesce", execute=execute)
    report = sched.run()
    assert report["status"] == "ok", report

    graph_report = report["graph"]
    assert graph_report["reconcile_passes"] >= 1
    # Exactly +1 per pass, never +1 per worker.
    assert graph_report["generation"] == graph_report["reconcile_passes"]
    assert graph_report["stale_intents_dropped"] == 0
    assert graph_report["failures"] == []

    # Every completion's change landed in some pass (union over passes).
    union = {path for entry in sched.reconcile_log
             for path in entry["files"]}
    assert union == {f"tests/w_{wid}.py" for wid in "ABCD"}
    # Each changed file became a graph node (all import `os`).
    assert {f"tests/w_{wid}.py" for wid in "ABCD"} <= set(
        sched.graph.nodes)

    # Pristine Git invariant: reconciliation stayed in-memory; the main
    # working tree is untouched (runtime state is gitignored).
    status = subprocess.run(["git", "status", "--porcelain"],
                            cwd=repo, capture_output=True,
                            text=True, check=True)
    assert status.stdout.strip() == ""
