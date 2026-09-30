# K.6.1 — Root Cause Analysis

Established by source-level evidence at HEAD `778e2f9`.

## R1 — T2 reverse omission: traversal direction, not missing graph data

**Observed**: `slice_context(FailureIdentity)` omitted `extract_failures` and
`verdict_for`.

**Evidence**:
1. `CodeGraph.edges` CONTAINS incoming edges to the `FailureIdentity` node:
   ```
   edges TO sym:asha.governance.delta:FailureIdentity:
     ('sym:asha.governance.delta:extract_failures', 'call')
     ('sym:asha.governance.delta:verdict_for', 'reference')
   ```
   (verified by direct inspection of `graph.edges`).
2. `codegraph.py` had ONLY `edges_from` — no `edges_to` method at all.
3. `closure()` (codegraph.py:353) walks `graph.edges_from(node)` exclusively:
   outgoing edges — dependencies of the node, never its consumers.
4. Therefore a leaf dataclass slice can only ever contain symbols the dataclass
   DEPENDS on. Its consumers (callers) are invisible to the traversal even
   though the edge data is present.

**Class**: C (traversal direction). Indexing and symbol resolution are fine;
the reverse relationship is stored but unreachable through the public API.

## R2 — T1 context bloat: full bodies inlined instead of stubs

**Observed**: B1 context for `collect_worker_evidence` = 10311 tokens (−48%,
larger than Grep).

**Evidence**:
1. Target body: 74 lines / 550 words — small.
2. Slice contained 208 dependency stubs; 136 were resolved symbol nodes.
3. `context_slicer.py:64`:
   ```python
   text = stub_for(fact)              # line 62: compact signature stub
   ...
   full_parts.append(fact.source if fact is not None else text)  # line 64
   ```
   The compact stub computed at line 62 is DISCARDED for resolved symbols;
   the FULL dependency source is appended instead. 136 full bodies = the
   token cost. Stub serialization existed but was only used for the
   `surgical_bytes` metric, never emitted.

**Class**: B (context expansion policy). The stub strategy is implemented
but bypassed at emission.

## R3 — T6/T4 symbol resolution: methods need qualified names

**Observed**: slicing `run` (a method) or `from_dict` surfaced
`UNKNOWN`/`not indexed`.

**Evidence**:
1. The indexer stores methods as `Class.method`: symbol list of
   `asha.governance.dag` includes `DAGCoordinator`, `DAGCoordinator.run`,
   `DAGCoordinator._run_one`, ... — bare `run` is NOT a module symbol.
2. Same for `asha.governance.delta`: `FailureIdentity`,
   `FailureIdentity.from_dict`, `FailureIdentity.to_dict`.
3. `ModuleIndex.symbol(name)` performs a name-repo lookup; a bare method name
   has no entry.

**Class**: A (indexing semantics) — the graph is complete, but the caller
must reference `Class.method` for methods. The K.6.1 benchmark uses qualified
names; documented as a caller contract, not a production bug (grep fell back
to the bare name per K.6 convention).

## R4 — No gap in close relation to T2: `from_dict` genuinely has no callers in the graph

**Observed**: reverse slice of `FailureIdentity.from_dict` = 72 tokens,
`INCOMPLETE` ("no dependencies resolved"), missing `to_dict`.

**Evidence**: `extract_failures` calls `FailureIdentity(name, ...)` — the
CONSTRUCTOR — not `from_dict`. No node in the graph references
`FailureIdentity.from_dict`. This is a true negative: the reverse traversal
correctly reports there are no upstream edges, rather than hallucinating
consumer context. Ground truth for T4 was corrected accordingly (removed
`extract_failures` from required symbols).