#!/usr/bin/env python3
"""Asha Phase 2.1 -- dependency projection: CodeGraph -> WorkerGraph.

Two-layer model preserved (task section 4):

    CodeGraph   = GraphState over files (dep_index facts); its
                  reconciliation and generation live in graph_state.
    WorkerGraph = per-worker predecessor sets = declared deps UNION
                  edges derived from CodeGraph facts through live
                  ownership; THIS is what TopologicalSorter consumes.
                  Never file paths.

Ownership policy (task section 6), stated exactly:

    owner(path) = LIVE workers whose declared_scope covers path
                  (conflict.covered -- the same glob semantics dispatch
                  uses); LIVE = not in `completed`, because historical
                  execution is not future topology.
    no live owner on either side          -> no derived edge
    same single live owner on both sides  -> no cross-worker edge
    exactly one live owner each, differing -> consumer depends on
                                              provider (Y depends on X)
    >= 2 live owners on an endpoint that takes part in a file edge
                                              -> every live owner is
                                                 UNCERTAIN: dispatch is
                                                 fail-closed blocked,
                                                 never guessed.

Failure semantics: derive() never raises -- ambiguity is data.
build_sorter() raises WorkerCycleError carrying the member set so the
caller can reject the CANDIDATE atomically: GraphState and WorkerGraph
are only ever published together (task sections 16/17).

Virtual analysis identity (sections 10/11): virtual_fingerprint() names
the mixed completed-worker output view deterministically. It is an
analysis identity ONLY -- never a git tree, never target_tree_sha, and
never integration or ship authority.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Any

from .conflict import covered


@dataclass(frozen=True)
class Derivation:
    """Pure projection result; the caller decides whether to publish."""

    edges: Mapping[str, set[str]]          # wid -> declared+derived preds
    declared: Mapping[str, list[str]]      # wid -> declared deps (kept)
    derived: tuple[tuple[str, str], ...]   # sorted (consumer, provider)
    uncertain: frozenset[str]              # fail-closed blocked worker ids
    ambiguous_paths: tuple[str, ...]       # the offending paths


class WorkerCycleError(Exception):
    """Worker-level cycle: candidate rejected, previous state retained."""

    def __init__(self, members: Sequence[str]) -> None:
        super().__init__("worker_graph cycle: " + ",".join(members))
        self.members = tuple(sorted(set(members)))


def derive(workers: list[dict[str, Any]],
           file_edges: Mapping[str, Iterable[str]],
           *, completed: Iterable[str]) -> Derivation:
    """Project file-level edges onto worker-level dependencies.

    Incremental by construction: it touches only the edges it is handed
    (the caller passes GraphState.edges -- already an incremental,
    reverse-closure-scoped structure), never a repository rescan.
    """
    done = frozenset(completed)
    alive = [worker for worker in workers if worker["id"] not in done]
    declared: dict[str, list[str]] = {
        str(worker["id"]): list(worker.get("deps") or [])
        for worker in workers}
    edges: dict[str, set[str]] = {
        wid: set(preds) for wid, preds in declared.items()}
    for worker in workers:
        edges.setdefault(str(worker["id"]), set())

    def live_owners(path: str) -> list[str]:
        owners: list[str] = []
        for worker in alive:
            scope = worker.get("declared_scope") or []
            if any(covered(path, str(entry)) for entry in scope):
                owners.append(str(worker["id"]))
        return owners

    derived: set[tuple[str, str]] = set()
    uncertain: set[str] = set()
    ambiguous: set[str] = set()
    for source, targets in file_edges.items():
        source_owners = live_owners(source)
        for target in targets:
            target_owners = live_owners(str(target))
            if not source_owners or not target_owners:
                continue          # one-sided ownership: no worker edge
            if len(source_owners) > 1:
                uncertain.update(source_owners)
                ambiguous.add(source)
                continue
            if len(target_owners) > 1:
                uncertain.update(target_owners)
                ambiguous.add(str(target))
                continue
            consumer, provider = source_owners[0], target_owners[0]
            if consumer == provider:
                continue          # same worker owns both: no cross edge
            edges[consumer].add(provider)
            derived.add((consumer, provider))
    return Derivation(
        edges=edges, declared=declared,
        derived=tuple(sorted(derived)),
        uncertain=frozenset(uncertain),
        ambiguous_paths=tuple(sorted(ambiguous)))


def build_sorter(edges: Mapping[str, Iterable[str]]
                 ) -> TopologicalSorter[str]:
    """Validate a candidate WorkerGraph; raise WorkerCycleError instead
    of ever constructing a partially-valid sorter (section 16)."""
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for node in sorted(edges):
        predecessors = [pred for pred in edges[node]
                        if pred in edges and pred != node]
        sorter.add(node, *predecessors)
    try:
        sorter.prepare()
    except CycleError as exc:
        members = (list(exc.args[1]) if len(exc.args) > 1
                   and isinstance(exc.args[1], list) else sorted(edges))
        raise WorkerCycleError(members) from None
    return sorter


def virtual_fingerprint(providers: Mapping[str, tuple[str, str]],
                        contents: Mapping[str, str | None]) -> str:
    """Deterministic identity of the analysis-only view over completed
    worker outputs (sections 10/11). Prefixed `virtual:` -- it can never
    be mistaken for, or used as, a git tree identity."""
    rows = []
    for path in sorted(contents):
        content = contents[path]
        digest = ("<deleted>" if content is None else
                  hashlib.sha256(content.encode("utf-8",
                                                errors="replace")).hexdigest())
        owner, tree = providers.get(path, ("<unknown>", "<unknown>"))
        rows.append([path, digest, owner, tree])
    blob = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    return "virtual:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = [
    "Derivation", "WorkerCycleError", "build_sorter", "derive",
    "virtual_fingerprint",
]
