#!/usr/bin/env python3
"""Phase 10 controlled negative tests against the real Asha engine.

Disposable clone of the benchmark target; one scenario at a time; the clone is
reset between scenarios. Every Asha call goes through the real MCP server via
asha_mcp.py, except S4/S5 which exercise internal boundaries that no MCP tool
exposes (worker-evidence and task-identity verification).

Run:  /home/amir/src/Asha/.venv/bin/python negcontrols.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BENCH = Path("/home/amir/bench/p10")
CLONE = BENCH / "neg" / "repo"
STATE = BENCH / "neg" / "state"
ASHA_PY = "/home/amir/src/Asha/.venv/bin/python"
PYD_PY = "/home/amir/src/detailist/pydetactl/.venv/bin/python"
ASHA_MCP = BENCH / "asha_mcp.py"


def run(cmd: list[str], cwd: Path, env: dict | None = None, timeout: int = 300):
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, **(env or {})})
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git(*args: str, cwd: Path | None = None) -> str:
    code, out, err = run(["git", *args], cwd or CLONE)
    if code != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {err or out}")
    return out


def reset_clone() -> None:
    git("reset", "-q", "--hard", "HEAD")
    git("clean", "-qfdx")
    git("config", "user.email", "bench@example.invalid")
    git("config", "user.name", "Bench")


def asha(tool: str, arguments: dict, *, expect_error: bool = False) -> dict:
    code, out, err = run([ASHA_PY, str(ASHA_MCP), "call", tool,
                          json.dumps(arguments)],
                         cwd=BENCH, env={"ASHA_BENCH_STATE": str(STATE)})
    payload: dict
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        payload = {"_raw": out}
    return {"exit": code, "ok": code == 0 and not expect_error,
            "payload": payload, "stderr": err[-400:]}


def pytest_clone() -> tuple[int, str]:
    code, out, _ = run([PYD_PY, "-m", "pytest", "tests", "-q"],
                       cwd=CLONE / "pydetactl")
    return code, out.splitlines()[-1] if out else ""


def scenario(label: str) -> dict:
    reset_clone()
    return {"scenario": label}


def main() -> int:
    results: list[dict] = []
    if CLONE.exists():
        shutil.rmtree(CLONE)
    CLONE.parent.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "-q", "--depth", "1",
         "file:///home/amir/src/detailist", str(CLONE)], cwd=BENCH.parent)
    reset_clone()
    base = git("rev-parse", "HEAD")

    # S1 -- a command that genuinely fails must never be reported as a pass.
    item = scenario("S1_verification_failure")
    item["asha"] = asha("asha_run_spec", {
        "root": str(CLONE), "apply": True,
        "spec_content": {"workers": [{
            "id": "w-fail", "deps": [], "declared_scope": ["pydetactl/README.md"],
            "reads": ["pydetactl/README.md"],
            "writes": ["pydetactl/README.md"],
            "cmd": ["/bin/sh", "-c", "exit 7"],
            "checks": ["/bin/sh -c 'exit 0'"],
        }]},
    })
    item["expected"] = "worker FAILED / not authorized / no sealed evidence"
    results.append(item)

    # S2 -- a write outside the declared scope must be refused, not sealed.
    item = scenario("S2_out_of_scope_write")
    item["asha"] = asha("asha_run_spec", {
        "root": str(CLONE), "apply": True,
        "spec_content": {"workers": [{
            "id": "w-scope", "deps": [],
            "declared_scope": ["pydetactl/README.md"],
            "reads": ["pydetactl/README.md"],
            "writes": ["pydetactl/README.md"],
            "cmd": ["/bin/sh", "-c",
                    "echo out >> pydetactl/src/utils/s3_resolver.py"],
        }]},
    })
    item["expected"] = "INVALID_EVIDENCE / scope violation, no success"
    results.append(item)

    # S3 -- a target that moves after review preparation must go stale.
    item = scenario("S3_stale_target")
    started = asha("asha_review", {"root": str(CLONE), "base": base, "head": base})
    run_id = (started["payload"] or {}).get("run", {}).get("run_id", "")
    packet = (started["payload"] or {}).get("packet", {})
    for _ in range(3):
        asha("asha_review_submit_stage", {
            "root": str(CLONE), "review_run_id": run_id,
            "stage": packet.get("stage"), "stage_id": packet.get("stage_id"),
            "verdict": "APPROVE", "summary": "prepared against a pinned target",
            "findings": [],
            "reviewer_metadata": {"host_client": "bench-host"},
        })
        nxt = asha("asha_review_next_stage",
                   {"root": str(CLONE), "run_id": run_id})
        packet = nxt["payload"] if nxt["exit"] == 0 else packet
    (CLONE / "pydetactl" / "README.md").write_text("# moved\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "move the target after review preparation")
    final = asha("asha_review_finalize", {"root": str(CLONE), "run_id": run_id})
    item["run_id"] = run_id
    item["asha"] = final
    item["expected"] = "STALE/BLOCKED, authorized_to_ship false"
    results.append(item)

    # S4 -- a sealed artifact moved onto another worker's file must be refused.
    item = scenario("S4_substituted_worker_evidence")
    code, out, err = run([ASHA_PY, "-c", (
        "import json,shutil,sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0,'/home/amir/src/Asha')\n"
        "from asha.governance.worker_execution import verify_worker_evidence\n"
        f"arts=sorted(Path('{STATE}').rglob('orchestrator/*/*.json'))\n"
        "if len(arts)<2: print(json.dumps({'error':'fewer than two artifacts',"
        "'found':[str(a) for a in arts]})); raise SystemExit(0)\n"
        "a,b=arts[0],arts[1]\n"
        "shutil.copyfile(a,b)\n"
        "try:\n"
        "    verify_worker_evidence(b, expected_worker_id=b.stem)\n"
        "    print(json.dumps({'detected':False}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'detected':True,'error':f'{type(exc).__name__}: {exc}'}))\n"
    )], cwd=BENCH)
    item["probe"] = {"exit": code, "stdout": out[-500:], "stderr": err[-300:]}
    item["expected"] = "detected: identity mismatch"
    results.append(item)

    # S5 -- an artifact presented under another task id must be refused.
    item = scenario("S5_invalid_task_identity")
    code, out, err = run([ASHA_PY, "-c", (
        "import json,shutil,sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0,'/home/amir/src/Asha')\n"
        "from asha.governance.worker_execution import verify_worker_evidence\n"
        f"arts=sorted(Path('{STATE}').rglob('orchestrator/*/*.json'))\n"
        "if not arts: print(json.dumps({'error':'no artifacts'})); raise SystemExit(0)\n"
        "a=arts[0]\n"
        "declared=json.loads(a.read_text())['task_id']\n"
        "try:\n"
        "    verify_worker_evidence(a, expected_task_id='task-that-never-ran')\n"
        "    print(json.dumps({'detected':False,'declared':declared}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'detected':True,'declared':declared,"
        "'error':f'{type(exc).__name__}: {exc}'}))\n"
    )], cwd=BENCH)
    item["probe"] = {"exit": code, "stdout": out[-500:], "stderr": err[-300:]}
    item["expected"] = "detected: task mismatch"
    results.append(item)

    # S6 -- an uninstalled tool must not be reported as a passing check.
    item = scenario("S6_missing_tool_is_not_a_pass")
    item["asha"] = asha("asha_run_spec", {
        "root": str(CLONE), "apply": True,
        "spec_content": {"workers": [{
            "id": "w-tool", "deps": [], "declared_scope": ["pydetactl/README.md"],
            "reads": ["pydetactl/README.md"],
            "writes": ["pydetactl/README.md"],
            "cmd": ["/bin/sh", "-c", "echo touched >> pydetactl/README.md"],
            "checks": ["definitely-not-installed-tool --run"],
        }]},
    })
    item["expected"] = "check failure surfaced, validation not PASS"
    results.append(item)

    # S7 -- genuinely valid work must not be blocked.
    item = scenario("S7_valid_work_not_blocked")
    (CLONE / "pydetactl" / "README.md").write_text("# touched\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "a clean, in-scope documentation change")
    head = git("rev-parse", "HEAD")
    code, tail = pytest_clone()
    item["pytest"] = {"exit": code, "tail": tail}
    started = asha("asha_review", {"root": str(CLONE), "base": base, "head": head})
    run_id = (started["payload"] or {}).get("run", {}).get("run_id", "")
    packet = (started["payload"] or {}).get("packet", {})
    for _ in range(3):
        asha("asha_review_submit_stage", {
            "root": str(CLONE), "review_run_id": run_id,
            "stage": packet.get("stage"), "stage_id": packet.get("stage_id"),
            "verdict": "APPROVE", "summary": "clean in-scope change",
            "findings": [],
            "reviewer_metadata": {"host_client": "bench-host"},
        })
        nxt = asha("asha_review_next_stage", {"root": str(CLONE), "run_id": run_id})
        packet = nxt["payload"] if nxt["exit"] == 0 else packet
    item["run_id"] = run_id
    item["asha"] = asha("asha_review_finalize", {"root": str(CLONE), "run_id": run_id})
    item["expected"] = "authorized_to_ship true (no false refusal)"
    results.append(item)

    print(json.dumps({"base": base, "results": results}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
