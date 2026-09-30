Exact commands used to reproduce:

# 1. Baseline env
cd /home/amir/src/Asha
.venv/bin/python --version   # 3.12.1
.venv/bin/pytest --version   # 7.4.4
.venv/bin/ruff --version     # 0.16.9
.venv/bin/mypy --version     # 2.3.1
git --version                # 2.30.2

# 2. Run the benchmark (warmup + 3 measured reps, T1-T7 x A/B/C)
.venv/bin/python benchmarks/k5/run_k5.py

# 3. T8 independent probe
.venv/bin/python benchmarks/k5/run_t8.py

# 4. Results
benchmarks/k5/results/k5-<runid>-all.json      # consolidated raw
benchmarks/k5/results/run-manifest.json        # env/spec hashes
benchmarks/k5/results/t8-probe.json            # T8
