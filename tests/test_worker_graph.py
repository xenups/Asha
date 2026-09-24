"""Phase 2.1 -- CodeGraph -> WorkerGraph projection (pure unit layer).

The gap this file pins down (proved from code before implementation):
GraphState.reconcile() published file-level topology with generation +1,
while GovernedScheduler.run() built its TopologicalSorter ONCE from
declared `deps` and never read self.graph again -- so a completed
worker's dependency changes never reached dispatch.

Ownership policy (documented exactly, task section 6):
  owner(path) = LIVE workers whose declared_scope covers path,
                where LIVE = not yet completed at derive time
                (historical execution is not future topology).
  no live owner on either side  -> no derived worker edge
  same live owner on both sides -> no cross-worker edge
  exactly one live owner each side, differing -> Y depends on X
  >= 2 live owners on an endpoint that participates in a file edge
      -> that endpoint's owners are UNCERTAIN (fail-closed block)
Declared deps are UNIONED with derived edges and never disappear.

TDD note: `asha.worker_graph` does not exist when this file first runs
(demonstrated collection failure = RED), then goes green.

Mapping to the required test list (section 18):
  1  test_derived_edges_appear_in_projection
  2  test_derived_edge_delays_consumer_worker        (rule-level)
  3  test_provider_completion_removes_derived_edge
  4  test_added_target_promotes_raw_edge             (readiness-level in
                                                      test_phase21 file)
  5  test_delete_puts_reverse_dependents_in_affected
  6  test_newly_created_target_promotes_edge
  7  test_rename_is_delete_plus_add
  8/9    existing tests/test_graph_state.py coalescing tests (green)
  10 existing test_stale_generation_intent_rejected + behavioral face
     in test_phase21_worker_dag.py
  14 test_worker_level_cycle_rejected_by_sorter
  15/16 scheduler faces in test_phase21_worker_dag.py
  17 test_unknown_reconcile_leaves_projection_untouched
  19 test_declared_dependencies_survive_projection
  20 the full existing suite stays green
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asha import dep_index, graph_state, worker_graph


def _w(wid: str, *, declared: list[str], deps: list[str] | None = None,
       ) -> dict:
    return {"id": wid, "deps": list(deps or []),
            "declared_scope": list(declared),
            "reads": [], "writes": [],
            "cmd": [sys.executable, "-c", "pass"]}


# -- ownership + derivation rules (sections 5/6) -----------------------------

def test_derived_edges_appear_in_projection() -> None:
    """#1: file facts cross worker scopes -> worker-level dependency."""
    workers = [_w("C", declared=["pkg/f.py"]),
               _w("P", declared=["pkg/g.py"])]
    edges = {"pkg/f.py": frozenset({"pkg/g.py"})}
    cand = worker_graph.derive(workers, edges, completed=frozenset())
    assert cand.edges["C"] == {"P"}  # consumer C depends on provider P
    assert cand.derived == (("C", "P"),)
    assert cand.uncertain == frozenset()


def test_derived_edge_delays_consumer_worker() -> None:
    """#2: the derived pair is exactly what blocks the consumer."""
    workers = [_w("C", declared=["pkg/f.py"]),
               _w("P", declared=["pkg/g.py"])]
    cand = worker_graph.derive(workers, {"pkg/f.py": frozenset({"pkg/g.py"})},
                               completed=frozenset())
    sorter = worker_graph.build_sorter(
        {wid: set(w.get("deps") or [])
         for wid, w in ((w["id"], w) for w in workers)}
        | dict(cand.edges))
    ready_first = list(sorter.get_ready())  # build_sorter already prepared
    assert ready_first == ["P"]  # C is not ready: it depends on P
    sorter.done("P")
    assert set(sorter.get_ready()) == {"C"}


def test_provider_completion_removes_derived_edge() -> None:
    """#3: provider completes -> its live claim dies -> derived edge gone
    (the two-level release path; the file-level fact may still exist)."""
    workers = [_w("C", declared=["pkg/f.py"]),
               _w("P", declared=["pkg/g.py"])]
    edges = {"pkg/f.py": frozenset({"pkg/g.py"})}
    before = worker_graph.derive(workers, edges, completed=frozenset())
    assert before.edges["C"] == {"P"}
    after = worker_graph.derive(workers, edges, completed=frozenset({"P"}))
    assert "P" not in after.edges.get("C", set())
    assert after.edges.get("C", set()) == set()


def test_no_owner_or_same_owner_makes_no_edge() -> None:
    workers = [_w("A", declared=["pkg/a.py"]),
               _w("B", declared=["pkg/b.py"])]
    # unowned endpoints: external node + absent scope -> no edge
    out = worker_graph.derive(
        workers, {"pkg/a.py": frozenset({"os"})}, completed=frozenset())
    assert out.edges.get("A", set()) == set()
    assert out.derived == ()
    # same owner on both sides -> no cross-worker edge
    same = worker_graph.derive(
        [_w("A", declared=["pkg/"])],
        {"pkg/a.py": frozenset({"pkg/b.py"})}, completed=frozenset())
    assert same.edges.get("A", set()) == set()
    assert same.derived == ()


def test_ambiguous_live_owners_fail_closed() -> None:
    """#6/section 6: >=2 live claimants on an edge endpoint = UNKNOWN."""
    workers = [_w("C1", declared=["pkg/f.py"]),
               _w("C2", declared=["pkg/f.py"]),
               _w("P", declared=["pkg/g.py"])]
    cand = worker_graph.derive(workers,
                               {"pkg/f.py": frozenset({"pkg/g.py"})},
                               completed=frozenset())
    assert cand.uncertain == frozenset({"C1", "C2"})
    assert cand.edges.get("C1", set()) == set()   # no invented edge
    assert cand.edges.get("C2", set()) == set()


def test_completed_claims_drop_before_ambiguity() -> None:
    """The enabler: broad completed worker's claim vanishes, so a single
    pending inner claimant owns the path unambiguously."""
    workers = [_w("W", declared=["pkg/"]),
               _w("C", declared=["pkg/f.py"]),
               _w("P", declared=["pkg/g.py"])]
    cand = worker_graph.derive(
        workers, {"pkg/f.py": frozenset({"pkg/g.py"})},
        completed=frozenset({"W"}))
    assert cand.uncertain == frozenset()
    assert cand.edges["C"] == {"P"}


def test_declared_dependencies_survive_projection() -> None:
    """#19: declared edges are unioned, never replaced by file facts."""
    workers = [_w("X", declared=["pkg/x.py"], deps=["Y"]),
               _w("Y", declared=["pkg/y.py"]),
               _w("Z", declared=["pkg/z.py"])]
    cand = worker_graph.derive(workers,
                               {"pkg/x.py": frozenset({"pkg/z.py"})},
                               completed=frozenset())
    assert "Y" in cand.edges["X"]     # declared preserved
    assert "Z" in cand.edges["X"]     # derived added
    assert cand.declared["X"] == ["Y"]
    assert cand.derived == (("X", "Z"),)


def test_worker_level_cycle_rejected_by_sorter() -> None:
    """#14: declared A dep B + derived B dep A -> candidate rejected."""
    workers = [_w("A", declared=["pkg/a.py"], deps=["B"]),
               _w("B", declared=["pkg/b.py"])]
    # file edge: B's src imports A's path -> B depends on A (derived)
    cand = worker_graph.derive(
        workers, {"pkg/b.py": frozenset({"pkg/a.py"})},
        completed=frozenset())
    combined = {wid: set(preds) for wid, preds in cand.edges.items()}
    with pytest.raises(worker_graph.WorkerCycleError) as exc:
        worker_graph.build_sorter(combined)
    assert set(exc.value.members) >= {"A", "B"}


def test_unknown_reconcile_leaves_projection_untouched() -> None:
    """#17: UNKNOWN never becomes EMPTY; a failed pass feeds the
    projection nothing (scheduler face in test_phase21)."""
    index = dep_index.DependencyIndex()
    state = graph_state.GraphState.empty()
    out = graph_state.reconcile(state, index,
                                {"x.py": "importlib.import_module('m')\n"})
    assert not out.ok
    # projection input == published state edges; nothing was published
    cand = worker_graph.derive([_w("A", declared=["x.py"])],
                               out.state.edges, completed=frozenset())
    assert cand.derived == ()


# -- CodeGraph invalidation: add / delete / rename (section 12) --------------

def test_newly_created_target_promotes_edge() -> None:
    """#6: unresolved raw target becomes a real dependency once the
    provider file exists and is analyzed."""
    index = dep_index.DependencyIndex()
    first = graph_state.reconcile(
        graph_state.GraphState.empty(), index,
        {"pkg/consumer.py": "import ghostmod\n"})
    assert first.ok
    assert "ghostmod" in first.state.edges["pkg/consumer.py"]  # raw leaf
    second = graph_state.reconcile(
        first.state, index, {"ghostmod.py": "VALUE = 1\n"})
    assert second.ok
    assert second.state.edges["pkg/consumer.py"] == frozenset(
        {"ghostmod.py"})
    assert "pkg/consumer.py" in second.affected  # promotion recorded


def test_delete_puts_reverse_dependents_in_affected() -> None:
    """#5: reverse closure over the deleted node."""
    index = dep_index.DependencyIndex()
    first = graph_state.reconcile(
        graph_state.GraphState.empty(), index,
        {"pkg/a.py": "import pkg.target\n",
         "pkg/target.py": "VALUE = 1\n"})
    assert first.ok
    second = graph_state.reconcile(first.state, index,
                                   {"pkg/target.py": None})
    assert second.ok
    assert "pkg/a.py" in second.affected
    assert "pkg/a.py" in second.state.edges  # dependent reconsidered


def test_rename_is_delete_plus_add() -> None:
    """#7: one batch {old: None, new: content}; no git rename magic."""
    index = dep_index.DependencyIndex()
    first = graph_state.reconcile(
        graph_state.GraphState.empty(), index,
        {"pkg/oldmod.py": "import ghost2\n"})
    assert first.ok
    second = graph_state.reconcile(
        first.state, index,
        {"pkg/oldmod.py": None, "ghost2.py": "VALUE = 2\n"})
    assert second.ok
    # deleted source's edges are gone
    assert "pkg/oldmod.py" not in second.state.edges
    # added target promoted on the surviving raw consumer... the old
    # source is gone; a second consumer proves the add half:
    third = graph_state.reconcile(
        second.state, index, {"pkg/consumer2.py": "import ghost2\n"})
    assert third.ok
    # ghost2.py is known now: no raw leaf, direct local edge
    assert third.state.edges["pkg/consumer2.py"] == frozenset(
        {"ghost2.py"})


# -- virtual analysis identity (section 10/11) -------------------------------

def test_virtual_fingerprint_is_deterministic_and_content_bound() -> None:
    providers = {"pkg/f.py": ("W1", "tree1"), "pkg/g.py": ("W2", "tree2")}
    contents = {"pkg/f.py": "import os\n", "pkg/g.py": "VALUE = 1\n"}
    first = worker_graph.virtual_fingerprint(providers, contents)
    again = worker_graph.virtual_fingerprint(providers, contents)
    assert first == again
    changed = dict(contents, **{"pkg/g.py": "VALUE = 2\n"})
    assert worker_graph.virtual_fingerprint(providers, changed) != first
    assert not first.startswith("git:")  # analysis identity, never a tree
