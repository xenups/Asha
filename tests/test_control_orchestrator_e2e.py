"""E2E integration suite for `control.py orchestrator` (governed entry).

Every test runs the REAL CLI as an external process against an isolated
tmp git repository -- no mocks, no in-process shortcuts. Mandated
scenarios: linear happy path, parallel-disjoint happy path, missing
transport gate, cyclic spec, scope violation, malformed spec (bad JSON
and corrupted schema).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CONTROL = REPO / ".jspace" / "control.py"
PY = sys.executable


# -- self-contained fixtures (house pattern: real scratch git repo) ---------

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True)
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(
        ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n")
    (repo / "README.md").write_text("# e2e fixture\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _write_cmd(target: str, body: str = "import os\n") -> list[str]:
    script = ("from pathlib import Path\n"
              f"Path({target!r}).write_text({body!r})\n")
    return [PY, "-c", script]


def _worker(wid: str, deps: list[str] | None = None,
            writes: list[str] | None = None,
            cmd: list[str] | None = None,
            scope: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": wid,
        "deps": deps or [],
        "declared_scope": ["tests/"] if scope is None else scope,
        "reads": [],
        "writes": writes or [],
        "cmd": [PY, "-c", "pass"] if cmd is None else cmd,
    }


def _spec(tmp_path: Path, task_id: str, workers: list[dict[str, Any]],
          raw: str | None = None) -> Path:
    path = tmp_path / f"spec-{task_id}.json"
    text = (raw if raw is not None else
            json.dumps({"task_id": task_id, "workers": workers}))
    path.write_text(text, encoding="utf-8")
    return path


def _control(repo: Path, spec: Path,
             transport: bool = True) -> subprocess.CompletedProcess[str]:
    argv = [PY, str(CONTROL)]
    if transport:
        argv += ["--transport", "local"]
    argv += ["--root", str(repo), "orchestrator", "--spec", str(spec)]
    return subprocess.run(argv, cwd=repo, capture_output=True, text=True,
                          timeout=600)


def _worktrees_dir(repo: Path) -> Path:
    return repo.parent / (repo.name + ".worktrees")


# -- 1. Happy path: linear DAG ------------------------------------------------

def test_linear_dag_happy_path(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    spec = _spec(tmp_path, "linear", [
        _worker("A", writes=["tests/a.py"], cmd=_write_cmd("tests/a.py")),
        _worker("B", deps=["A"], writes=["tests/b.py"],
                cmd=_write_cmd("tests/b.py", "import pathlib\n")),
    ])
    asha_before = _git(REPO, "status", "--porcelain")

    proc = _control(repo, spec)

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)  # stdout is exactly one JSON report
    assert report["status"] == "ok"
    assert report["completed"] == ["A", "B"]  # linear order enforced
    assert all(entry["state"] == "DONE" for entry in report["states"].values())
    assert Path(report["evidence"]["A"]).is_file()
    assert Path(report["evidence"]["B"]).is_file()
    assert report["graph"]["generation"] >= 1  # reconciliation ran

    # zero leakage: fixture clean, worktree root gone, no stray files in
    # the parent, and the Asha repo itself byte-identical to before
    assert _git(repo, "status", "--porcelain") == ""
    assert not _worktrees_dir(repo).exists()
    assert {p.name for p in tmp_path.iterdir()} <= {"repo", spec.name}
    assert _git(REPO, "status", "--porcelain") == asha_before


# -- 2. Happy path: parallel disjoint execution -------------------------------

def test_parallel_disjoint_execution(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # Mutual rendezvous over a SHARED dir (worktrees are isolated, so
    # workers cannot see each other's in-tree files -- that isolation is
    # itself the invariant). Each worker publishes its own sentinel
    # FIRST, then waits up to 10s for the other's. Sequential dispatch
    # would leave the first waiter alone -> timeout -> exit 3 -> FAILED,
    # so this test reaching exit 0 PROVES concurrent dispatch.
    shared = tmp_path / "shared"
    shared.mkdir()

    def rendezvous(mine: str, theirs: str, out: str, body: str) -> str:
        return (
            "import sys, time\n"
            "from pathlib import Path\n"
            f"shared = Path({str(shared)!r})\n"
            f"(shared / {mine!r}).write_text('ready')\n"
            "deadline = time.time() + 10\n"
            "while time.time() < deadline:\n"
            f"    if (shared / {theirs!r}).exists():\n"
            f"        Path({out!r}).write_text({body!r})\n"
            "        sys.exit(0)\n"
            "    time.sleep(0.1)\n"
            "sys.exit(3)\n"
        )

    spec = _spec(tmp_path, "parallel", [
        _worker("A", writes=["tests/a.py"],
                cmd=[PY, "-c", rendezvous("a_ready", "b_ready", "tests/a.py",
                                          "import os\n")]),
        _worker("B", writes=["tests/b.py"],
                cmd=[PY, "-c", rendezvous("b_ready", "a_ready", "tests/b.py",
                                          "import pathlib\n")]),
    ])

    proc = _control(repo, spec)

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["status"] == "ok"
    # disjoint pair: both complete; parallel order is legitimately
    # arbitrary, so assert the set plus per-worker DONE, not a sequence
    assert set(report["completed"]) == {"A", "B"}
    assert report["states"]["A"]["state"] == "DONE"
    assert report["states"]["B"]["state"] == "DONE"
    assert set(report["worktrees"]) == {"A", "B"}  # isolated per worker
    assert not report["deferral_events"]  # proven-disjoint, never deferred
    assert _git(repo, "status", "--porcelain") == ""


# -- 3. Fail-closed: missing transport gate -----------------------------------

def test_missing_transport_gate_fails_closed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # fully valid spec so the GATE is the only possible failure
    spec = _spec(tmp_path, "gate", [
        _worker("A", writes=["tests/a.py"], cmd=_write_cmd("tests/a.py"))])

    proc = _control(repo, spec, transport=False)

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "TRANSPORT GATE" in combined
    assert "--transport" in combined
    # refused BEFORE execution: no report payload, no worktree, clean tree
    assert '"status"' not in proc.stdout
    assert not _worktrees_dir(repo).exists()
    assert _git(repo, "status", "--porcelain") == ""


# -- 4. Fail-closed: cyclic dependency ----------------------------------------

def test_cyclic_spec_fails_closed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    spec = _spec(tmp_path, "cycle", [
        _worker("A", deps=["B"], writes=["tests/a.py"],
                cmd=_write_cmd("tests/a.py")),
        _worker("B", deps=["A"], writes=["tests/b.py"],
                cmd=_write_cmd("tests/b.py")),
    ])

    proc = _control(repo, spec)

    assert proc.returncode == 1
    report = json.loads(proc.stdout)  # structured diagnostic, not a crash
    assert report["status"] == "failed"
    assert report["reason"] == "cycle"
    assert set(report["cycle"]) == {"A", "B"}
    assert report["states"]["A"] == {"state": "FAILED", "reason": "cycle"}
    assert report["states"]["B"] == {"state": "FAILED", "reason": "cycle"}
    assert report["completed"] == []  # zero partial execution
    assert not _worktrees_dir(repo).exists()
    assert _git(repo, "status", "--porcelain") == ""


# -- 5. Fail-closed: scope violation ------------------------------------------

def test_scope_violation_fails_closed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # declared tests/ but escapes to the repo root on purpose
    spec = _spec(tmp_path, "escape", [
        _worker("A", writes=["escaped.txt"], scope=["tests/"],
                cmd=_write_cmd("escaped.txt"))])

    proc = _control(repo, spec)

    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert report["status"] == "failed"
    entry = report["states"]["A"]
    assert entry["state"] == "INVALID_EVIDENCE"
    assert entry["reason"].startswith("scope_violation")
    assert "escaped.txt" in entry["reason"]
    # no tree corruption: escape exists only inside the (removed) worktree
    assert not (repo / "escaped.txt").exists()
    assert _git(repo, "status", "--porcelain") == ""
    assert not _worktrees_dir(repo).exists()


# -- 6. Fail-closed: malformed specification ----------------------------------

def test_unparseable_spec_json_fails_clean(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    spec = _spec(tmp_path, "broken", [], raw="{this is not json")

    proc = _control(repo, spec)

    assert proc.returncode == 1
    assert "ORCHESTRATOR ERROR" in proc.stderr
    assert "invalid spec json" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert '"status"' not in proc.stdout  # no partial report
    assert not _worktrees_dir(repo).exists()
    assert _git(repo, "status", "--porcelain") == ""


def test_corrupted_schema_spec_fails_clean(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # parseable JSON, structurally invalid schema (workers not a list)
    spec = _spec(tmp_path, "schema", [],
                 raw=json.dumps({"task_id": "bad",
                                 "workers": "not-a-list"}))

    proc = _control(repo, spec)

    assert proc.returncode == 1
    assert "ORCHESTRATOR ERROR" in proc.stderr
    assert "spec.workers" in proc.stderr  # explicit schema diagnostic
    assert "Traceback" not in proc.stderr
    assert '"status"' not in proc.stdout
    assert not _worktrees_dir(repo).exists()
    assert _git(repo, "status", "--porcelain") == ""
