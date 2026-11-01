"""Phase 12 (F4) live acceptance over the real stdio MCP server + Ship Gate.

One server process per call (``p11_mcp.py`` locates Asha via ASHA_HOME), real
review lifecycle, real gate. Scenarios:

  S1  healthy monorepo (root + service suites pass)      -> APPROVED
  S2  root passes, service test genuinely fails           -> FAILED, unauthorized
  S3  candidate deletes the required service suite        -> unauthorized
  S4  service environment poisoned (bad .venv interpreter) -> unauthorized
  S5  service pytest collection error                     -> unauthorized
  S7  untracked user file in the worktree                 -> REFUSED

S6 (evidence from another checkout) is contract-level: it is proven by
tests/test_monorepo_service_verification.py::test_g_evidence_cannot_be_reused_
from_another_checkout, because the live MCP surface has no way to inject a
foreign evidence document without editing the journal (out of scope).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

BENCH = Path("/home/amir/bench/p12")
ASHA_PY = os.environ.get("ASHA_PY", "/home/amir/src/Asha/.venv/bin/python")
CLIENT = BENCH / "p11_mcp.py"
BASE = "899ab1c3692067c4f13c28365cc19ce8183624f1"
REAL_VENV = "/home/amir/bench/p12/svcvenv"
ASHA_HOME = os.environ.get("ASHA_HOME", "/home/amir/asha-f4p")
STATE = Path(os.environ.get("ASHA_BENCH_STATE", str(BENCH / "state")))


def run(cmd, cwd=BENCH):
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env["ASHA_HOME"] = ASHA_HOME
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=2400, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git(root: Path, *args: str) -> str:
    code, out, err = run(["git", *args], cwd=root)
    if code:
        raise SystemExit(f"git {args}: {err or out}")
    return out


def asha(tool: str, args: dict) -> dict:
    _code, out, err = run([ASHA_PY, str(CLIENT), "call", tool,
                           json.dumps(args)])
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"_raw": out, "_stderr": err[-400:]}


def clone(name: str) -> Path:
    root = BENCH / name
    if root.exists():
        shutil.rmtree(root)
    code, out, err = run(["git", "clone", "-q", "--depth", "1",
                          "file:///home/amir/src/detailist", str(root)],
                         cwd=BENCH.parent)
    if code:
        raise SystemExit(err or out)
    git(root, "fetch", "-q", "origin", BASE, "--depth", "1")
    git(root, "checkout", "-q", BASE)
    git(root, "config", "user.email", "bench@example.invalid")
    git(root, "config", "user.name", "Bench")
    # service deps live outside the clone: bind the service interpreter so the
    # suite can actually import its dependencies (the way a developer's
    # checkout is laid out).
    if os.environ.get("P12_SVC_VENV") == "1":
        venv = root / "pydetactl" / ".venv"
        if not venv.exists():
            venv.symlink_to(REAL_VENV)
    # The target's own suite writes its plan into the CWD (detailist PR #297,
    # still open). Carry that fix as baseline so the gate is not refused for
    # an artifact the candidate did not create.
    fixed = Path("/home/amir/detailist-f3/pydetactl/tests/test_plan.py")
    if fixed.is_file():
        (root / "pydetactl" / "tests" / "test_plan.py").write_text(
            fixed.read_text(encoding="utf-8"), encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "baseline: plan test writes to tmp_path")
    return root


def prepare(root: Path, head: str) -> dict:
    started = asha("asha_review", {"root": str(root), "base": BASE,
                                   "head": head})
    if "run" not in started:
        return {"error": started}
    run_id = started["run"]["run_id"]
    packet = started["packet"]
    for _ in range(3):
        asha("asha_review_submit_stage", {
            "root": str(root), "review_run_id": run_id,
            "stage": packet["stage"], "stage_id": packet["stage_id"],
            "verdict": "APPROVE", "summary": "phase 12 acceptance",
            "findings": [], "reviewer_metadata": {"host_client": "phase12"}})
        nxt = asha("asha_review_next_stage",
                   {"root": str(root), "review_run_id": run_id})
        packet = nxt if isinstance(nxt, dict) and "stage" in nxt else packet
    return {"run_id": run_id}


def finalize(root: Path, run_id: str) -> dict:
    final = asha("asha_review_finalize", {"root": str(root),
                                          "review_run_id": run_id})
    gate = final.get("gate") or {}
    return {"state": final.get("state"), "decision": final.get("decision"),
            "gate_state": gate.get("gate_state"),
            "authorized_to_ship": gate.get("authorized_to_ship"),
            "reason": final.get("blocking_reason"),
            "snapshot_id": gate.get("snapshot_id"),
            "checks": gate.get("checks")}


def evidence_checks(root: Path, run_id: str) -> list[dict]:
    """The gate's own check records, read from the sealed stage artifact."""
    found: list[dict] = []
    for stage in STATE.glob(f"*/reviews/{run_id}/stages/SHIP_GATE.json"):
        try:
            payload = json.loads(stage.read_text())
            artifact = json.loads(Path(payload["artifact"]).read_text())
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
        sealed = Path(stage.parents[3] / "evidence.json")
        checks = artifact.get("checks") or []
        if sealed.is_file():
            try:
                checks = json.loads(sealed.read_text()).get("checks") or checks
            except (OSError, json.JSONDecodeError):
                pass
        for entry in checks:
            if str(entry.get("name", "")).startswith("pytest"):
                found.append({"name": entry.get("name"),
                              "status": entry.get("status"),
                              "exit_code": entry.get("exit_code"),
                              "tail": (entry.get("output_tail") or "")[-120:]})
    return found


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def scenario(name: str, mutate, after_commit=None) -> dict:
    root = clone("x_" + name)
    mutate(root)
    head = commit(root, "phase 12 " + name.replace("_", " "))
    files = git(root, "diff", "--name-only", BASE, "HEAD")
    if after_commit is not None:
        after_commit(root)
    prepared = prepare(root, head)
    if "run_id" not in prepared:
        return {"scenario": name, "prepare": prepared, "changed": files,
                "head": head}
    record = {"scenario": name, "changed": files, "head": head,
              "run_id": prepared["run_id"]}
    record.update(finalize(root, prepared["run_id"]))
    record["pytest_evidence"] = evidence_checks(root, prepared["run_id"])
    record["tree"] = git(root, "rev-parse", head + "^{tree}")
    return record


def m_healthy(root: Path) -> None:
    target = root / "pydetactl" / "src" / "asha_health_probe.py"
    target.write_text('"""A harmless module: the suites stay green."""\n\n'
                      'VALUE = 1\n')


def m_service_failure(root: Path) -> None:
    (root / "pydetactl" / "tests" / "test_deliberate.py").write_text(
        'def test_deliberate_failure():\n'
        '    """Lint-clean, and it cannot hold."""\n'
        '    assert 1 + 1 == 3\n')


def m_suite_deleted(root: Path) -> None:
    shutil.rmtree(root / "pydetactl" / "tests")


def m_env_poisoned(root: Path) -> None:
    m_healthy(root)
    venv = root / "pydetactl" / ".venv"
    if venv.is_symlink() or venv.is_file():
        venv.unlink()
    elif venv.exists():
        shutil.rmtree(venv)
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /nonexistent\n")
    fake = venv / "bin" / "python"
    fake.write_text("#!/bin/sh\nexit 127\n")
    fake.chmod(0o755)


def m_collection_error(root: Path) -> None:
    (root / "pydetactl" / "tests" / "test_broken.py").write_text(
        "import a_module_that_is_not_installed\n")


def m_untracked(root: Path) -> None:
    """A committed healthy candidate; the user file arrives after the commit."""
    m_healthy(root)


def after_untracked(root: Path) -> None:
    (root / "pydetactl" / "notes.txt").write_text("mine\n")


SCENARIOS = [
    ("healthy", m_healthy),
    ("service_failure", m_service_failure),
    ("suite_deleted", m_suite_deleted),
    ("env_poisoned", m_env_poisoned),
    ("collection_error", m_collection_error),
    ("untracked", m_untracked),
]


def main() -> int:
    BENCH.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    _, info, info_err = run([ASHA_PY, str(CLIENT), "info"])
    identity = {"stdout": info[-1200:], "stderr": info_err[-300:]}
    out: dict = {"asha_home": ASHA_HOME, "state": str(STATE),
                 "server": identity}
    only = [n for n in os.environ.get("ONLY", "").split(",") if n]
    hooks = {"untracked": after_untracked}
    for name, mutate in SCENARIOS:
        if only and name not in only:
            continue
        out[name] = scenario(name, mutate, hooks.get(name))
        print(json.dumps({name: {k: out[name].get(k) for k in
                                 ("state", "decision", "gate_state",
                                  "authorized_to_ship")}}))
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
