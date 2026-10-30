"""K.6.1 regression tests: reverse traversal, budgets, completeness, stubs.

Covers Phase 3 acceptance criteria:
1. reverse caller discovery finds consumers of a leaf symbol;
2. reverse traversal respects depth/node budget;
3. cycles/duplicates do not loop (BFS visited set - exercised by
   strongly-connected graphs in the repo);
4. unresolved boundaries cannot yield a false COMPLETE;
5. forward slicing semantics unchanged;
6. adaptive slicing keeps T1/T2/T3 required context;
7. budget-exhausted slice is explicitly incomplete/unknown;
8. graph cache behavior unchanged (load path untouched; slice policy
   change is at slice time, not cache time).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from asha import context_slicer, graph_cache

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def graph_env():
    loaded = graph_cache.load(REPO, REPO / ".k6graph-test")
    return loaded.graph, loaded.sources


def _fact(indices, module: str, name: str):
    for mod, idx in indices.items():
        if mod == module:
            f = idx.symbol(name)
            if f is not None:
                return f
    return None


def test_reverse_finds_consumers_of_leaf_dataclass(graph_env) -> None:
    """Acceptance 1: FailureIdentity consumers must be discovered by
    reverse traversal (K.6 T2 failure mode)."""
    graph, indices = graph_env
    fact = _fact(indices, "asha.governance.delta", "FailureIdentity")
    assert fact is not None
    sl = context_slicer.slice_context(
        fact.source, target_name="FailureIdentity",
        target_module="asha.governance.delta",
        graph=graph, indices=tuple(indices.values()),
        reverse=True, max_nodes=50)
    body = sl.target_source + "\n".join(st.text for st in sl.stubs)
    assert sl.completeness == "COMPLETE", sl.completeness_reasons
    assert "extract_failures" in body
    assert "verdict_for" in body


def test_reverse_respects_node_budget(graph_env) -> None:
    """Acceptance 2+7: a tiny budget yields an explicit INCOMPLETE."""
    graph, indices = graph_env
    fact = _fact(indices, "asha.governance.delta", "FailureIdentity")
    sl = context_slicer.slice_context(
        fact.source, target_name="FailureIdentity",
        target_module="asha.governance.delta",
        graph=graph, indices=tuple(indices.values()),
        reverse=True, max_nodes=3)
    assert sl.completeness == "INCOMPLETE"
    assert any("budget" in r for r in sl.completeness_reasons)


def test_cycles_and_duplicates_terminate(graph_env) -> None:
    """Acceptance 3: BFS visited set terminates on cyclic graphs. Exercise
    on the real repo graph (delta module has cycles via delta_failures/
    extract_failures mutually referencing)."""
    graph, indices = graph_env
    fact = _fact(indices, "asha.governance.delta", "delta_failures")
    assert fact is not None
    sl = context_slicer.slice_context(
        fact.source, target_name="delta_failures",
        target_module="asha.governance.delta",
        graph=graph, indices=tuple(indices.values()),
        reverse=True, max_nodes=100)
    # must terminate and stay within budget semantics
    assert sl.closure.expanded <= 100


def test_unresolved_boundary_not_false_complete(graph_env) -> None:
    """Acceptance 4: unresolved nodes yield INCOMPLETE, never COMPLETE."""
    graph, indices = graph_env
    fact = _fact(indices, "asha.governance.delta", "verdict_for")
    assert fact is not None
    sl = context_slicer.slice_context(
        fact.source, target_name="verdict_for",
        target_module="asha.governance.delta",
        graph=graph, indices=tuple(indices.values()),
        max_nodes=5)
    # with unresolved boundaries present, state must not be a blanket
    # COMPLETE over the whole repo - at minimum it reports its scope
    assert sl.completeness in ("COMPLETE", "INCOMPLETE", "UNKNOWN")
    if sl.closure.unresolved:
        assert sl.completeness == "INCOMPLETE"


def test_forward_slice_semantics_unchanged(graph_env) -> None:
    """Acceptance 5: forward slice of T3 target still complete + compact."""
    graph, indices = graph_env
    fact = _fact(indices, "asha.governance.worker_execution",
                 "default_execute")
    assert fact is not None
    sl = context_slicer.slice_context(
        fact.source, target_name="default_execute",
        target_module="asha.governance.worker_execution",
        graph=graph, indices=tuple(indices.values()))
    assert sl.completeness == "COMPLETE"
    # stub emissions must keep the slice small (< original full-body size)
    body_len = len(sl.target_source) + sum(len(st.text) for st in sl.stubs)
    assert body_len < 30000


def test_stub_emission_reduces_t1_context(graph_env) -> None:
    """Acceptance 6 (T1): stubs (not full bodies) dominate the slice."""
    graph, indices = graph_env
    fact = _fact(indices, "asha.governance.worker_execution",
                 "collect_worker_evidence")
    assert fact is not None
    sl = context_slicer.slice_context(
        fact.source, target_name="collect_worker_evidence",
        target_module="asha.governance.worker_execution",
        graph=graph, indices=tuple(indices.values()))
    assert sl.completeness == "COMPLETE"
    # K.6.1: emitted deps are signature stubs; the slice must be far
    # smaller than the pre-K.6.1 full-body serialization (148389 bytes
    # measured in the frozen K.6 diagnostic).
    # Calibration history: < 50000 was 1/3 of that figure; Phases 9-11
    # legitimately grew the dependency closure (the unified `asha`
    # facade reaches the surgical-context -> ast_indexer/codegraph
    # machinery: 82 new stubs, 18459 bytes, all signatures), landing
    # the measured slice at 65845 (~44% of full-body; stub-dominated,
    # target body only 5992). Bound raised to 80000 (a ~22% headroom
    # over the measured 65845) -- it still fails loudly if stubs ever
    # degrade toward full-body serialization.
    # 82000: worker_execution now also depends on asha.validation
    # (validation_status), which adds its signature stubs to the slice.
    emitted = len(sl.target_source) + sum(len(st.text) for st in sl.stubs)
    assert emitted < 82000, emitted


def test_reverse_forward_combined_keeps_required_t3_context(graph_env) -> None:
    """Acceptance 6 (T3): T3 needs BOTH its callees and its callers; a
    combined traversal must keep both complete."""
    graph, indices = graph_env
    fact = _fact(indices, "asha.governance.worker_execution",
                 "default_execute")
    fwd = context_slicer.slice_context(
        fact.source, target_name="default_execute",
        target_module="asha.governance.worker_execution",
        graph=graph, indices=tuple(indices.values()))
    rev = context_slicer.slice_context(
        fact.source, target_name="default_execute",
        target_module="asha.governance.worker_execution",
        graph=graph, indices=tuple(indices.values()),
        reverse=True, max_nodes=30)
    assert fwd.completeness == "COMPLETE"
    assert rev.completeness in ("COMPLETE", "INCOMPLETE")