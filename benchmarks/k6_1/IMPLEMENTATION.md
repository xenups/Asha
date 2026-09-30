# K.6.1 — Implementation

Minimal, evidence-backed changes to two files. No governance, test-execution,
or cache-isolation behavior touched.

## asha/codegraph.py

1. **`CodeGraph.edges_to(node)`** — new method returning incoming edges
   (reverse dependencies / callers). Edge data already existed; this exposes
   it. (Fixes R1 traversal direction.)
2. **`closure(..., *, reverse=False, max_nodes=None)`** — traversal direction
   is now explicit: forward (dependencies, default, unchanged) or reverse
   (callers/consumers). Reverse follows the edge SOURCE side (callers), never
   the target. `max_nodes` bounds expansion; when exhausted the result sets
   `budget_exhausted=True`.
3. **`ClosureResult.budget_exhausted: bool = False`** — new field, default
   preserves the existing constructor/behavior for all current callers.
4. BFS visited-set already existed — cycles and duplicate edges terminate;
   no new cycle risk from reverse traversal (same set discipline).

## asha/context_slicer.py

1. **`slice_context(..., *, reverse=False, max_nodes=None)`** — forwards to
   `closure`, enabling caller discovery for leaf symbols. (Fixes R1.)
2. **Stub emission for resolved symbols** — `full_parts.append(text)` (the
   stub) instead of `fact.source` (the full body). The stub strategy that was
   already computed is now the emitted context. (Fixes R2.)
3. **`ContextSlice.completeness` + `completeness_reasons`** — explicit
   completeness contract:
   - `INCOMPLETE` if `budget_exhausted`, OR unresolved nodes present, OR
     zero dependencies resolved — with the concrete reason.
   - `COMPLETE` otherwise (slice policy finished within budget, no
     unresolved nodes).
   - `UNKNOWN` is NOT emitted by the slicer itself (a missing/invalid target
     surfaces at lookup, which the caller reports as UNKNOWN).

## API compatibility

- All existing `closure()` callers: unchanged (new kwargs default to the old
  behavior).
- All existing `slice_context()` callers: unchanged defaults; the EMITTED
  context changed from full dependency bodies to stubs — this is the
  intended fix, and `full_bytes`/`surgical_bytes` now agree (both stub-based).
  `symbol_count` unchanged.
- `ClosureResult` gained an optional field with a default: pickle-compatible
  in `graph_cache`.

## Determinism

No RNG, no clock, no wall-clock waits. Same inputs → identical outputs.
Node ordering in stubs follows BFS order = deterministic.