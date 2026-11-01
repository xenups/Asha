#!/usr/bin/env python3
"""Phase 10 negative controls, second pass (corrected tool arguments).

Reuses the disposable clone and state from negcontrols.py. Scenarios:
  S3 stale target after review preparation
  S4 substituted worker-evidence artifact
  S5 artifact presented under another task id
  S6b a worker edit that breaks the suite must not validate as success
  S7 a genuinely valid change must still be authorized
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

BENCH = Path("/home/amir/bench/p10")
CLONE = BENCH / "neg" / "repo"
STATE = BENCH / "neg" / "state"
ASHA_PY = "/home/amir/src/Asha/.venv/bin/python"
ASHA_MCP = BENCH / "asha_mcp.py"
BASE = "899ab1c3692067c4f13c28365cc19ce8183624f1"


def run(cmd, cwd=BENCH, extra_env=None, timeout=600):
    env = dict(os.environ)
    env["ASHA_BENCH_STATE"] = str(STATE)
    env.update(extra_env or {})
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git(*args):
    code, out, err = run(["git", *args], cwd=CLONE)
    if code:
        raise RuntimeError(f"git {args} -> {err or out}")
    return out


def reset():
    git("reset", "-q", "--hard")
    git("clean", "-qfdx")
    git("config", "user.email", "bench@example.invalid")
    git("config", "user.name", "Bench")


def asha(tool, args):
    code, out, err = run([ASHA_PY, str(ASHA_MCP), "call", tool, json.dumps(args)])
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        payload = {"_raw": out}
    return {"exit": code, "payload": payload, "stderr": err[-300:]}


def summarize(result):
    payload = result.get("payload") or {}
    keep = {k: payload.get(k) for k in
            ("status", "authorized_to_ship", "gate_state", "state", "decision",
             "reason", "blocking_reason", "states", "validation_status")
            if k in payload}
    return keep


def review_cycle(base, head, verdict="APPROVE", summary="reviewed"):
    started = asha("asha_review", {"root": str(CLONE), "base": base, "head": head})
    body = started["payload"] or {}
    run_id = body.get("run", {}).get("run_id", "")
    packet = body.get("packet", {})
    stages = []
    for _ in range(3):
        submitted = asha("asha_review_submit_stage", {
            "root": str(CLONE), "review_run_id": run_id,
            "stage": packet.get("stage"), "stage_id": packet.get("stage_id"),
            "verdict": verdict, "summary": summary, "findings": [],
            "reviewer_metadata": {"host_client": "bench-host"},
        })
        stages.append(submitted["payload"].get("status")
                      if isinstance(submitted["payload"], dict) else None)
        nxt = asha("asha_review_next_stage",
                   {"root": str(CLONE), "review_run_id": run_id})
        packet = nxt["payload"] if nxt["exit"] == 0 else packet
    final = asha("asha_review_finalize",
                 {"root": str(CLONE), "review_run_id": run_id})
    status = asha("asha_review_status", {"root": str(CLONE), "run_id": run_id})
    return run_id, stages, final, status


def main() -> int:
    results = []

    # S3 -- target moves after the stages were prepared.
    reset()
    run_id, stages, final, status = review_cycle(BASE, BASE)
    (CLONE / "pydetactl" / "README.md").write_text("# moved\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "move the target after review preparation")
    later = asha("asha_review_finalize", {"root": str(CLONE), "review_run_id": run_id})
    results.append({
        "scenario": "S3_stale_target", "run_id": run_id, "stages": stages,
        "finalize_before_move": summarize(final),
        "finalize_after_move": summarize(later),
        "status_after_move": summarize(status),
        "expected": "stale/blocked, authorized_to_ship false",
    })

    # S4 -- a sealed artifact moved onto another worker's file.
    artifacts = sorted(Path(STATE).rglob("orchestrator/*/w-tool.json"))
    if not artifacts:
        results.append({"scenario": "S4_substituted_worker_evidence",
                        "probe": {"error": "no sealed artifact to substitute"}})
    else:
        source = artifacts[0]
        target = source.with_name("w-other.json")
        target.write_bytes(source.read_bytes())
        code, out, err = run([ASHA_PY, "-c", (
            "import json,sys\n"
            "sys.path.insert(0,'/home/amir/src/Asha')\n"
            "from asha.governance.worker_execution import verify_worker_evidence\n"
            f"try:\n"
            f"    verify_worker_evidence(r'{target}', expected_worker_id='w-other')\n"
            f"    print(json.dumps({{'detected': False}}))\n"
            f"except Exception as exc:\n"
            f"    print(json.dumps({{'detected': True, 'error': f'{{type(exc).__name__}}: {{exc}}'}}))\n"
        )])
        results.append({"scenario": "S4_substituted_worker_evidence",
                        "artifact": str(source), "probe": {"exit": code,
                        "stdout": out[-400:], "stderr": err[-200:]},
                        "expected": "detected: worker identity mismatch"})

    # S5 -- artifact presented under another task id.
    if artifacts:
        code, out, err = run([ASHA_PY, "-c", (
            "import json,sys\n"
            "sys.path.insert(0,'/home/amir/src/Asha')\n"
            "from asha.governance.worker_execution import verify_worker_evidence\n"
            f"try:\n"
            f"    verify_worker_evidence(r'{artifacts[0]}', expected_task_id='task-never-ran')\n"
            f"    print(json.dumps({{'detected': False}}))\n"
            f"except Exception as exc:\n"
            f"    print(json.dumps({{'detected': True, 'error': f'{{type(exc).__name__}}: {{exc}}'}}))\n"
        )])
        results.append({"scenario": "S5_invalid_task_identity",
                        "probe": {"exit": code, "stdout": out[-400:],
                                  "stderr": err[-200:]},
                        "expected": "detected: task mismatch"})

    # S6b -- a worker edit that breaks the suite must not be validated as success.
    reset()
    broken = asha("asha_run_spec", {
        "root": str(CLONE), "apply": True,
        "spec_content": {"workers": [{
            "id": "w-regress", "deps": [],
            "declared_scope": ["pydetactl/src/utils/s3_resolver.py"],
            "reads": ["pydetactl/src/utils/s3_resolver.py"],
            "writes": ["pydetactl/src/utils/s3_resolver.py"],
            "cmd": ["/bin/sh", "-c",
                    "printf 'def broken(:\\n' >> pydetactl/src/utils/s3_resolver.py"],
        }]},
    })
    results.append({"scenario": "S6b_regression_not_success",
                    "asha": summarize(broken),
                    "states": (broken["payload"] or {}).get("states"),
                    "expected": "validation/comparison failure, no PASS"})

    # S7 -- valid, in-scope work must still be authorized.
    reset()
    (CLONE / "pydetactl" / "README.md").write_text("# touched\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "a clean, in-scope documentation change")
    head = git("rev-parse", "HEAD")
    run_id, stages, final, status = review_cycle(BASE, head,
                                                 summary="clean in-scope change")
    results.append({"scenario": "S7_valid_work_not_blocked", "run_id": run_id,
                    "stages": stages, "finalize": summarize(final),
                    "status": summarize(status),
                    "expected": "authorized_to_ship true"})

    print(json.dumps({"results": results}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
