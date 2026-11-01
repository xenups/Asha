#!/usr/bin/env python3
"""Assemble the Phase 10 raw evidence bundle with checksums.

For every (task, condition): the change patch, diffstat, the final commit id,
an independent pytest run, and the independent acceptance probe. Everything is
regenerated on the host rather than copied from an agent report.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

BENCH = Path("/home/amir/bench/p10")
RAW = BENCH / "raw"
BASE = "899ab1c3692067c4f13c28365cc19ce8183624f1"
PYD_PY = "/home/amir/src/detailist/pydetactl/.venv/bin/python"

PAIRS = [("T1", "t1_base"), ("T1", "t1_asha"),
         ("T2", "t2_base"), ("T2", "t2_asha"),
         ("T3", "t3_base"), ("T3", "t3_asha")]


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=900)


def main() -> int:
    if RAW.exists():
        shutil.rmtree(RAW)
    RAW.mkdir(parents=True)
    index = {}
    for task, name in PAIRS:
        worktree = BENCH / name
        out = RAW / name
        out.mkdir()
        head = run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()
        (out / "commit.txt").write_text(head + "\n", encoding="utf-8")
        patch = run(["git", "diff", f"{BASE}..HEAD"], worktree).stdout
        (out / "change.patch").write_text(patch, encoding="utf-8")
        stat = run(["git", "diff", "--stat", f"{BASE}..HEAD"], worktree).stdout
        (out / "diffstat.txt").write_text(stat, encoding="utf-8")

        pyd = worktree / "pydetactl"
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        pytest_run = subprocess.run([PYD_PY, "-m", "pytest", "tests", "-q"],
                                    cwd=str(pyd), capture_output=True,
                                    text=True, timeout=900, env=env)
        (out / "pytest.log").write_text(
            pytest_run.stdout + pytest_run.stderr, encoding="utf-8")
        acceptance = subprocess.run(
            [PYD_PY, str(BENCH / "verify_acceptance.py"), task],
            cwd=str(pyd), capture_output=True, text=True, timeout=600, env=env)
        (out / "acceptance.json").write_text(acceptance.stdout, encoding="utf-8")
        index[name] = {
            "task": task, "commit": head,
            "pytest_exit": pytest_run.returncode,
            "pytest_tail": (pytest_run.stdout.strip().splitlines() or [""])[-1],
            "acceptance_exit": acceptance.returncode,
            "acceptance": acceptance.stdout.strip(),
        }

    manifest = {}
    for path in sorted(RAW.rglob("*")):
        if path.is_file():
            manifest[str(path.relative_to(RAW))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    (RAW / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in manifest.items()),
        encoding="utf-8")
    (RAW / "index.json").write_text(json.dumps(index, indent=1),
                                    encoding="utf-8")
    print(json.dumps({"files": len(manifest), "index": index}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
