# EXACT_COMMANDS — K.6 reproduce

# Part 1 audit (evidence files)
cat benchmarks/k6/CACHE_AUDIT.md

# Part 2 benchmark
.venv/bin/python benchmarks/k6/k6_harness.py
cat benchmarks/k6/results/k6_results.json

# Manifest hashes
sha256sum benchmarks/k6/FROZEN_SPEC.md benchmarks/k6/k6_harness.py

# Invalidate graph cache between runs (cold rebuild)
rm -rf .k6graph
