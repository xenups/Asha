"""K.5.2 runner — executes the frozen protocol T1-T7 (+T8 separately)
against Paths A/B/C, writes raw JSON telemetry + aggregated report.

Usage: python benchmarks/k5/run_k5.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.k5 import k5_harness as H


def load_spec() -> dict:
    spec = H.SPEC.read_text(encoding="utf-8")
    return {"spec_sha256": hashlib.sha256(spec.encode()).hexdigest(),
            "spec_bytes": len(spec)}


# --------------------------------------------------------------------------
# task scenario builders
# --------------------------------------------------------------------------
def svc_repo(name: str, failing: list[str] | None = None,
             two_services: bool = False) -> Path:
    """Minimal python repo; optional pre-existing failing tests."""
    failing = failing or []
    files = {
        "pyproject.toml": (
            "[project]\nname = \"k5\"\nversion = \"0.1.0\"\n"
            "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n"
            "[tool.ruff]\nline-length = 88\n"),
        "app.py": "def f():\n    return 1\n",
    }
    if two_services:
        files["services/compiler/app.py"] = "def c():\n    return 1\n"
        files["services/compose_orchestrator/app.py"] = "def o():\n    return 2\n"
    tests = ["import app\n\n\ndef test_f():\n    assert app.f() == 1\n"]
    for i, t in enumerate(failing):
        tests.append(f"def test_old_{i}():\n    assert False  # pre-existing\n")
    files["tests/test_app.py"] = "\n".join(tests)
    return H.mkrepo(name, files)


def worker_for(scope: list[str], cmd: list[str], writes: list[str]) -> dict:
    return {"id": "w1", "deps": [], "declared_scope": scope,
            "reads": list(scope), "writes": writes, "cmd": cmd}


NEW_TEST = ("from pathlib import Path;"
            "Path('tests/test_new.py').write_text("
            "'def test_new_broken():\\n    assert False\\n')")

TASKS: dict[str, dict] = {
    "T1": {
        "kind": "clean pass, no-op",
        "build": lambda: svc_repo("t1"),
        "worker": lambda r: worker_for(["tests/*.py", "app.py"],
                                       [sys.executable, "-c", "pass"], []),
        "patch": {"ops": []},
        "expected": {"A": ("DONE", "CLEAN_PASS"),
                     "B": ("DONE", "CLEAN_PASS"),
                     "C": ("DONE", "CLEAN_PASS")},
    },
    "T2": {
        "kind": "new regression (runtime change)",
        "build": lambda: svc_repo("t2"),
        "worker": lambda r: worker_for(
            ["app.py"],
            [sys.executable, "-c",
             ("from pathlib import Path;"
              "Path('app.py').write_text("
              "'def f():\\n    return 2\\n')")],
            ["app.py"]),
        "patch": {"ops": [{"path": "app.py",
                           "content": "def f():\n    return 2\n"}]},
        "expected": {"A": ("BLOCKED", "REJECT_BLIND"),
                     "B": ("BLOCKED", "REJECT_NEW_REGRESSION"),
                     "C": ("BLOCKED", "REJECT_NEW_REGRESSION")},
    },
    "T3": {
        "kind": "pre-existing only, no-op",
        "build": lambda: svc_repo("t3", failing=["a", "b", "c"]),
        "worker": lambda r: worker_for(["tests/*.py"],
                                       [sys.executable, "-c", "pass"], []),
        "patch": {"ops": []},
        "expected": {"A": ("BLOCKED", "REJECT_BLIND"),
                     "B": ("BLOCKED", "REJECT_BLIND"),
                     "C": ("DONE", "CLEAN_PASS")},
    },
    "T4": {
        "kind": "pre-existing + new (runtime change)",
        "build": lambda: svc_repo("t4", failing=["a", "b", "c"]),
        "worker": lambda r: worker_for(
            ["app.py"],
            [sys.executable, "-c",
             ("from pathlib import Path;"
              "Path('app.py').write_text("
              "'def f():\\n    return 2\\n')")],
            ["app.py"]),
        "patch": {"ops": [{"path": "app.py",
                           "content": "def f():\n    return 2\n"}]},
        "expected": {"A": ("BLOCKED", "REJECT_BLIND"),
                     "B": ("BLOCKED", "REJECT_NEW_REGRESSION"),
                     "C": ("BLOCKED", "REJECT_NEW_REGRESSION")},
    },
    "T5": {
        "kind": "scope bleed",
        "build": lambda: svc_repo("t5", two_services=True),
        "worker": lambda r: worker_for(
            ["services/compiler/*.py"],
            [sys.executable, "-c",
             ("from pathlib import Path;"
              "Path('services/compose_orchestrator/app.py')"
              ".write_text('def o():\\n    return 99\\n')")],
            ["services/compose_orchestrator/app.py"]),
        "patch": {"ops": [{"path": "services/compose_orchestrator/app.py",
                           "content": "def o():\n    return 99\n"}]},
        "expected": {"A": ("DONE", "CLEAN_PASS"),   # blind: no scope gate
                     "B": ("BLOCKED", "REJECT_SCOPE_VIOLATION"),
                     "C": ("BLOCKED", "REJECT_SCOPE_VIOLATION")},
    },
    "T6": {
        "kind": "cross-service ambiguity (same-service worker)",
        "build": lambda: svc_repo("t6", two_services=True),
        "worker": lambda r: worker_for(
            ["services/compiler/*.py"],
            [sys.executable, "-c", "pass"], []),
        "patch": {"ops": []},
        "expected": {"A": ("DONE", "CLEAN_PASS"),
                     "B": ("DONE", "CLEAN_PASS"),
                     "C": ("DONE", "CLEAN_PASS")},
    },
    "T7": {
        "kind": "missing venv, fallback disabled",
        "build": lambda: svc_repo("t7", two_services=True),
        "worker": lambda r: worker_for(
            ["services/compiler/*.py"],
            [sys.executable, "-c", "pass"], []),
        "patch": {"ops": []},
        "expected": {"A": ("DONE", "CLEAN_PASS"),
                     "B": ("DONE", "CLEAN_PASS"),
                     "C": ("BLOCKED", "REJECT_ENV_VIOLATION")},
    },
}


def env_for_path(path: str) -> dict:
    return {"PYTHON": H.REPO_ROOT / ".venv/bin/python",
            "ALLOW_FALLBACK": os.environ.get(
                "ASHA_ALLOW_SYSTEM_PYTHON_FALLBACK", "unset")}


def run_one(task: dict, path: str, root: Path, worker: dict,
            patch: dict, task_id: str) -> dict:
    if path == "A":
        return H.path_a(root, patch)
    if path == "B":
        return H.path_b(root, worker, patch, task_id)
    res, _ev = H.path_c(root, worker, task_id)
    return res


def make_record(run_id: str, task_id: str, path: str, task: dict,
                root: Path, result: dict, env: dict, patch: dict) -> dict:
    exp_v, exp_d = task["expected"][path]
    obs_v = result["observed_execution_verdict"]
    obs_d = result["observed_detection_classification"]
    metrics = result.get("metrics", {})
    return {
        "benchmark_run_id": run_id,
        "task_id": task_id,
        "path_id": path,
        "expected_execution_verdict": exp_v,
        "expected_detection_classification": exp_d,
        "observed_execution_verdict": obs_v,
        "observed_detection_classification": obs_d,
        "policy_compliance": (exp_v == obs_v) and (exp_d == obs_d),
        "metrics": metrics,
        "repo": {"root": str(root), "head": H.git(root, "rev-parse", "HEAD")},
        "patch_sha256": H.patch_hash(patch),
        "environment": env,
        "evidence_path": result.get("_evidence_path"),
        "state_detail": {k: v for k, v in result.get("_state", {}).items()
                         if k not in ("baseline_failures",
                                      "current_failures", "delta_failures")},
        "raw": {k: v for k, v in result.items() if k.startswith("_")},
    }


def main() -> None:
    spec = load_spec()
    run_id = "k5-" + time.strftime("%Y%m%d-%H%M%S")
    os.environ["ASHA_STATE_DIR"] = str(H.TMP / ".jspace")
    if H.TMP.exists():
        shutil.rmtree(H.TMP)
    H.TMP.mkdir(parents=True)
    H.OUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "benchmark_run_id": run_id,
        "spec_sha256": spec["spec_sha256"],
        "spec_bytes": spec["spec_bytes"],
        "repo_head": H.git(H.REPO_ROOT, "rev-parse", "HEAD"),
        "commit_time": H.git(H.REPO_ROOT, "log", "-1", "--format=%cI"),
        "tools": {
            "python": subprocess.run([sys.executable, "--version"],
                                     capture_output=True, text=True).stdout.strip(),
            "pytest": subprocess.run(
                [str(H.REPO_ROOT / ".venv/bin/pytest"), "--version"],
                capture_output=True, text=True).stdout.strip().split("\n")[0],
            "git": subprocess.run(["git", "--version"], capture_output=True,
                                  text=True).stdout.strip(),
            "ruff": subprocess.run(
                [str(H.REPO_ROOT / ".venv/bin/ruff"), "--version"],
                capture_output=True, text=True).stdout.strip(),
            "mypy": subprocess.run(
                [str(H.REPO_ROOT / ".venv/bin/mypy"), "--version"],
                capture_output=True, text=True).stdout.strip(),
        },
        "harness_revision": H.git(H.REPO_ROOT, "rev-parse", "--short", "HEAD"),
        "repetitions": H.REPS,
        "warmup": H.WARMUP,
    }
    (H.OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    records: list[dict] = []
    for task_id, task in TASKS.items():
        for path in ("A", "B", "C"):
            for rep in range(H.WARMUP + H.REPS):
                root = task["build"]()
                worker = task["worker"](root)
                # warmup run (timing discarded)
                if path == "C":
                    H.path_c(root, worker, f"{task_id}-warmup")
                else:
                    run_one(task, path, root, worker, task["patch"],
                            f"{task_id}-warmup")
                # measured run
                root2 = task["build"]()
                worker2 = task["worker"](root2)
                if path == "C":
                    # T7: remove venv + disable fallback for C only
                    if task_id == "T7":
                        for d in ("services/compiler/.venv",
                                  "services/compose_orchestrator/.venv"):
                            shutil.rmtree(root2 / d, ignore_errors=True)
                        os.environ["ASHA_ALLOW_SYSTEM_PYTHON_FALLBACK"] = "false"
                    res, _ev = H.path_c(root2, worker2,
                                       f"{task_id}-{path}-{rep}")
                    if task_id == "T7":
                        os.environ.pop("ASHA_ALLOW_SYSTEM_PYTHON_FALLBACK", None)
                else:
                    res = run_one(task, path, root2, worker2,
                                      task["patch"], f"{task_id}-{path}-{rep}")
                rec = make_record(run_id, task_id, path, task, root2, res,
                                  env_for_path(path), task["patch"])
                rec["rep"] = rep
                rec["measurement_number"] = rep  # 0 = warmup
                records.append(rec)
                (H.OUT / f"{run_id}-{task_id}-{path}-{rep}.json").write_text(
                    json.dumps(rec, indent=2, default=str), encoding="utf-8")

    (H.OUT / f"{run_id}-all.json").write_text(
        json.dumps(records, indent=2, default=str), encoding="utf-8")
    print(f"wrote {len(records)} records to {H.OUT}/{run_id}-all.json")
    # quick safety summary (no timing)
    for task_id in TASKS:
        for path in ("A", "B", "C"):
            recs = [r for r in records if r["task_id"] == task_id
                    and r["path_id"] == path and r["measurement_number"] > 0]
            for r in recs:
                print(f"{task_id} {path}: obs=({r['observed_execution_verdict']},"
                      f"{r['observed_detection_classification']}) "
                      f"compliance={r['policy_compliance']}")


if __name__ == "__main__":
    main()