# K.6.1 — Exact Commands

All commands run on `baltic.void-star.co:2220` as `amir`, repo
`/home/amir/src/Asha`, venv `.venv`.

## 1. Baseline reproduction (K.6 frozen harness)

```bash
git rev-parse HEAD                    # 778e2f9 (post-K.6)
rm -rf .k6graph
.venv/bin/python benchmarks/k6/k6_harness.py
# → T1: fwd=10311 (-48%); T2: fwd=186 missing [extract_failures, verdict_for];
#   T3: fwd=1248 (-86.5%)
```

## 2. Diagnostics

```bash
.venv/bin/python k61_diag.py   # edges TO FailureIdentity exist (R1)
.venv/bin/python k61_diag2.py  # 208 stubs, 136 full bodies dominate (R2)
```

## 3. Apply fixes

```bash
.venv/bin/python /tmp/k61_fix_a2.py    # edges_to + closure(reverse,max_nodes)
.venv/bin/python /tmp/k61_fix_a3b.py   # ClosureResult.budget_exhausted
.venv/bin/python /tmp/k61_fix_c.py     # reverse follows edge.source
.venv/bin/python /tmp/k61_fix_b2.py    # slicer: reverse, stub emission,
                                       # completeness state
```

## 4. Verify + tests

```bash
rm -rf .k6graph-test
env PYTHONPATH=. .venv/bin/pytest tests/test_k61_codegraph.py -q   # 7 passed
env PYTHONPATH=. .venv/bin/pytest tests/test_delta_check.py \
    tests/test_k53_stale_pyc_regression.py -q                      # 18 passed
.venv/bin/ruff check asha/codegraph.py asha/context_slicer.py      # clean
```

## 5. Benchmark

```bash
rm -rf .k6graph
.venv/bin/python benchmarks/k6_1/run_k6_1.py   # → results/results.json
.venv/bin/python benchmarks/k6_1/run_agent.py  # → NOT_PERFORMED (no API key)
```

## 6. Artifacts

```bash
.venv/bin/python k6_1_manifest.py   # manifests all k6_1 artifacts
git status --short                  # confirm tree
```