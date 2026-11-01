#!/usr/bin/env python3
"""Phase 10 S3 variants: ref-pinned target vs SHA-pinned target.

V1: review head by ref name ('HEAD'), then the branch moves -> expect STALE.
V2: review head by explicit SHA, then HEAD moves -> what does finalize say?
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


def run(cmd, cwd=BENCH):
    env = dict(os.environ)
    env["ASHA_BENCH_STATE"] = str(STATE)
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=600, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git(*args):
    code, out, err = run(["git", *args], cwd=CLONE)
    if code:
        raise SystemExit(f"git {args}: {err or out}")
    return out


def asha(tool, args):
    code, out, err = run([ASHA_PY, str(ASHA_MCP), "call", tool, json.dumps(args)])
    try:
        return {"exit": code, "payload": json.loads(out)}
    except json.JSONDecodeError:
        return {"exit": code, "payload": {"_raw": out}, "stderr": err[-200:]}


def prepare():
    git("reset", "-q", "--hard")
    git("clean", "-qfdx")
    git("config", "user.email", "bench@example.invalid")
    git("config", "user.name", "Bench")
    (CLONE / "pydetactl" / "README.md").write_text("# prepared\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "the change under review")
    return git("rev-parse", "HEAD")


def move():
    (CLONE / "pydetactl" / "README.md").write_text("# moved\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "move the target after preparation")
    return git("rev-parse", "HEAD")


def drive(base, head):
    started = asha("asha_review", {"root": str(CLONE), "base": base, "head": head})
    body = started["payload"]
    run_id = body.get("run", {}).get("run_id", "")
    packet = body.get("packet", {})
    target = body.get("run", {}).get("target", {})
    for _ in range(3):
        asha("asha_review_submit_stage", {
            "root": str(CLONE), "review_run_id": run_id,
            "stage": packet.get("stage"), "stage_id": packet.get("stage_id"),
            "verdict": "APPROVE", "summary": "reviewed",
            "findings": [], "reviewer_metadata": {"host_client": "bench-host"},
        })
        nxt = asha("asha_review_next_stage",
                   {"root": str(CLONE), "review_run_id": run_id})
        packet = nxt["payload"] if nxt["exit"] == 0 else packet
    return run_id, target


def finalize(run_id):
    payload = asha("asha_review_finalize",
                   {"root": str(CLONE), "review_run_id": run_id})["payload"]
    return {k: payload.get(k) for k in ("state", "decision", "approved",
                                        "reasons", "blocking_reason")}, \
        payload.get("gate")


out = {}

head_sha = prepare()
run_id, target = drive(BASE, "HEAD")
out["V1_ref_pinned"] = {"run_id": run_id, "target": target,
                        "head_ref": target.get("head_ref"),
                        "head_sha": target.get("head_sha")}
out["V1_ref_pinned"]["head_after_move"] = move()
out["V1_ref_pinned"]["finalize"], out["V1_ref_pinned"]["gate"] = finalize(run_id)

head_sha = prepare()
run_id, target = drive(BASE, head_sha)
out["V2_sha_pinned"] = {"run_id": run_id, "head_ref": target.get("head_ref"),
                        "head_sha": target.get("head_sha")}
out["V2_sha_pinned"]["head_after_move"] = move()
out["V2_sha_pinned"]["finalize"], out["V2_sha_pinned"]["gate"] = finalize(run_id)

print(json.dumps(out, indent=1))
