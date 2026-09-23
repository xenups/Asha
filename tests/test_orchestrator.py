"""Tests for the Phase 1 Orchestrator (governed worker scheduling).

Every test builds a genuine scratch git repository and exercises real git
worktrees where practical; hooks are used only for deterministic
concurrency/failure control, never to bypass the dispatch, scope,
evidence or cleanup invariants under test.

Invariant map (task section 11.3):
    1  independent ready workers dispatch concurrently
    2  dependent worker never dispatches before upstream evidence is DONE
    3  write/write conflict defers, then runs after completion
    4  write/read conflict defers
    5  read/read never conflicts
    6  UNKNOWN read/write sets are never treated as safe
    7  deferred worker is reconsidered after the conflicting one completes
    8  failed worker never reaches TopologicalSorter.done()
    9  evidence with wrong/missing tree identity cannot complete
    10 cycle detection fails closed
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / ".hermes" / "tools"
CONTROL = REPO_ROOT / ".jspace" / "control.py"
ORCHESTRATOR = TOOLS / "orchestrator.py"
PY = sys.executable

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import evidence  # (TOOLS must be on sys.path before these imports)
import orchestrator

GITIGNORE = (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
             ".mypy_cache/\n.ruff_cache/\n")

BASELINE = {
    ".gitignore": GITIGNORE,
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    "pkg/leaf.py": (
        "def _helper(x):\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def area(x):\n"
        "    return x * x\n"
    ),
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in BASELINE.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _worker(wid: str, *, deps: tuple[str, ...] = (),
            declared: tuple[str, ...] | None = ("tests/",),
            reads: tuple[str, ...] | None = (),
            writes: tuple[str, ...] | None = (),
            cmd: list[str] | None = None) -> dict:
    """Worker builder. reads/writes default to a KNOWN empty set; pass
    None explicitly to model UNKNOWN (the spec's fail-closed case)."""
    return {
        "id": wid,
        "deps": list(deps),
        "declared_scope": list(declared) if declared is not None else None,
        "reads": list(reads) if reads is not None else None,
        "writes": list(writes) if writes is not None else None,
        "cmd": cmd or [PY, "-c", "pass"],
    }


def _write_cmd(rel: str) -> list[str]:
    code = (f"from pathlib import Path; Path({rel!r}).write_text("
            f"'# written by fixture worker\\n')")
    return [PY, "-c", code]


def _assert_ok(report: dict) -> None:
    assert report["status"] == "ok", (report["states"], report["reason"])


class _Barrier:
    """Deterministic concurrency probe: every expected participant must
    arrive within the timeout or the hook fails the run loudly."""

    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.started: list[str] = []
        self._event = threading.Event()
        self._lock = threading.Lock()

    def arrive(self, worker_id: str) -> bool:
        with self._lock:
            self.started.append(worker_id)
            if len(self.started) >= self.expected:
                self._event.set()
        return self._event.wait(timeout=10)


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _conflict_runner_hook(box: dict, events: list):
    """First-started worker finishes ONLY after the conflict gate actually
    deferred its peer; if no deferral happens, the run fails loudly."""
    started: list[str] = []

    def execute(worker: dict, worktree: Path):
        wid = worker["id"]
        events.append((wid, "start"))
        if not started:
            started.append(wid)
            observed = _wait_for(lambda: bool(
                box["sched"].deferral_events))
            events.append((wid, "end"))
            return 0 if observed else 1
        events.append((wid, "end"))
        return 0

    return execute


def _assert_defer_then_run(events: list, deferrals: list,
                           labels: tuple[str, ...]) -> tuple[str, str]:
    """Direction-agnostic shape: runner start/end, then deferred
    start/end; exactly one deferral with an expected conflict label."""
    assert len(events) == 4, events
    assert [phase for _, phase in events] == ["start", "end", "start", "end"]
    runner, deferred = events[0][0], events[2][0]
    assert runner != deferred, events
    assert len(deferrals) == 1, deferrals
    assert deferrals[0]["worker"] == deferred, deferrals
    assert any(label in deferrals[0]["reason"] for label in labels), \
        deferrals[0]["reason"]
    return runner, deferred


# ---- 11.3.1: independent ready workers run concurrently -------------------

def test_independent_workers_dispatch_concurrently(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    barrier = _Barrier(2)
    workers = [
        _worker("A", writes=["tests/a_probe.py"]),
        _worker("B", writes=["tests/b_probe.py"]),
    ]
    sched = orchestrator.GovernedScheduler(
        repo, workers, task_id="t-concurrent",
        execute=lambda w, p: 0 if barrier.arrive(w["id"]) else 1)
    report = sched.run()
    _assert_ok(report)
    # both hooks had to be live simultaneously or the barrier times out
    assert set(barrier.started) == {"A", "B"}, barrier.started
    assert report["deferral_events"] == [], report["deferral_events"]
    assert set(report["completed"]) == {"A", "B"}
    assert set(report["evidence"]) == {"A", "B"}


# ---- 11.3.2: dependent worker waits for upstream evidence -----------------

def test_dependent_worker_waits_for_evidence_completion(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    events: list[str] = []
    box: dict = {}

    def execute(worker: dict, worktree: Path):
        wid = worker["id"]
        if wid == "A":
            # C must not be dispatchable while A is running
            assert box["sched"].states["C"]["state"] == "PENDING", \
                box["sched"].states
            events.append("A_run")
            return 0
        # C starts only after A is DONE *and* its evidence exists on disk
        assert box["sched"].states["A"]["state"] == "DONE"
        evi = box["sched"].evidence_paths.get("A")
        assert evi and Path(evi).is_file(), box["sched"].evidence_paths
        events.append("C_run")
        return 0

    workers = [_worker("A", writes=["tests/a.py"]),
               _worker("C", deps=["A"], writes=["tests/c.py"])]
    sched = orchestrator.GovernedScheduler(repo, workers, task_id="t-dep",
                                           execute=execute)
    box["sched"] = sched
    report = sched.run()
    _assert_ok(report)
    assert events == ["A_run", "C_run"], events
    assert report["completed"] == ["A", "C"], report["completed"]


# ---- 11.3.3 + 7: write/write defers, then reconsidered --------------------

def test_write_write_conflict_defers_then_runs(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    events: list = []
    box: dict = {}
    workers = [
        _worker("A", writes=["tests/shared.py"], reads=["tests/shared.py"]),
        _worker("B", writes=["tests/shared.py"]),
    ]
    sched = orchestrator.GovernedScheduler(
        repo, workers, task_id="t-ww",
        execute=_conflict_runner_hook(box, events))
    box["sched"] = sched
    report = sched.run()
    _assert_ok(report)
    _runner, deferred = _assert_defer_then_run(
        events, report["deferral_events"], ("write_write_overlap",))
    # deferred peer genuinely ran only after the runner finished
    assert (deferred, "start") in events
    assert set(report["completed"]) == {"A", "B"}
    assert report["reason"] is None


# ---- 11.3.4: write/read conflict defers -----------------------------------

def test_write_read_conflict_defers(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    events: list = []
    box: dict = {}
    workers = [
        _worker("A", writes=["tests/x.py"]),
        _worker("B", reads=["tests/x.py"], writes=["tests/y.py"]),
    ]
    sched = orchestrator.GovernedScheduler(
        repo, workers, task_id="t-wr",
        execute=_conflict_runner_hook(box, events))
    box["sched"] = sched
    report = sched.run()
    _assert_ok(report)
    _assert_defer_then_run(events, report["deferral_events"],
                           ("write_read_overlap", "read_write_overlap"))
    assert set(report["completed"]) == {"A", "B"}


# ---- 11.3.5: read/read never conflicts ------------------------------------

def test_read_read_never_conflicts(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    barrier = _Barrier(2)
    workers = [
        _worker("A", reads=["tests/shared_spec.py"],
                writes=["tests/a_out.py"]),
        _worker("B", reads=["tests/shared_spec.py"],
                writes=["tests/b_out.py"]),
    ]
    sched = orchestrator.GovernedScheduler(
        repo, workers, task_id="t-rr",
        execute=lambda w, p: 0 if barrier.arrive(w["id"]) else 1)
    report = sched.run()
    _assert_ok(report)
    assert set(barrier.started) == {"A", "B"}, barrier.started
    assert report["deferral_events"] == [], report["deferral_events"]


# ---- 11.3.6: UNKNOWN read/write sets are never proven safe ----------------

def test_unknown_read_set_never_treated_safe(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    events: list = []
    box: dict = {}
    workers = [
        # A: reads UNKNOWN (None), known-empty writes
        _worker("A", reads=None, writes=()),
        # B writes a concrete file: safety vs A's reads is unprovable
        _worker("B", writes=["tests/y.py"]),
    ]
    sched = orchestrator.GovernedScheduler(
        repo, workers, task_id="t-unknown",
        execute=_conflict_runner_hook(box, events))
    box["sched"] = sched
    report = sched.run()
    _assert_ok(report)
    _assert_defer_then_run(
        events, report["deferral_events"], ("unknown_set",))
    # UNKNOWN was deferred, not downgraded to an empty set
    assert "unknown" in report["deferral_events"][0]["reason"]
    assert set(report["completed"]) == {"A", "B"}


# ---- 11.3.7: deferred worker reconsidered; scheduler stays live -----------

def test_deferred_reconsidered_and_scheduler_stays_live(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    events: list = []
    box: dict = {}
    barrier = _Barrier(2)  # runner + independent C
    runner_seen: list[str] = []

    def execute(worker: dict, worktree: Path):
        wid = worker["id"]
        if wid == "C":  # independent worker must proceed during the stall
            events.append((wid, "start"))
            ok = barrier.arrive(wid)
            events.append((wid, "end"))
            return 0 if ok else 1
        events.append((wid, "start"))
        if not runner_seen:  # whichever of A/B dispatched first
            runner_seen.append(wid)
            ok = barrier.arrive(wid)
            ok = _wait_for(lambda: bool(
                box["sched"].deferral_events)) and ok
            events.append((wid, "end"))
            return 0 if ok else 1
        events.append((wid, "end"))
        return 0

    workers = [
        _worker("A", writes=["tests/shared.py"]),
        _worker("B", writes=["tests/shared.py"]),
        _worker("C", writes=["tests/c_independent.py"]),
    ]
    sched = orchestrator.GovernedScheduler(repo, workers, task_id="t-reconsider",
                                           execute=execute)
    box["sched"] = sched
    report = sched.run()
    _assert_ok(report)
    assert len(report["deferral_events"]) == 1, report["deferral_events"]
    deferred = report["deferral_events"][0]["worker"]
    assert deferred in ("A", "B")
    other = "B" if deferred == "A" else "A"
    # deferred worker started only after the conflicting runner ended
    assert events.index((deferred, "start")) > events.index((other, "end")), \
        events
    # independent C ran during the conflict window (scheduler not stalled)
    assert "C" in barrier.started, barrier.started
    assert set(report["completed"]) == {"A", "B", "C"}


# ---- 11.3.8: failed worker never reaches done() ---------------------------

def test_failed_worker_blocks_dependents_without_done(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ran: list[str] = []

    def execute(worker: dict, worktree: Path):
        ran.append(worker["id"])
        return 1 if worker["id"] == "A" else 0

    workers = [_worker("A"), _worker("C", deps=["A"])]
    sched = orchestrator.GovernedScheduler(repo, workers, task_id="t-fail",
                                           execute=execute)
    report = sched.run()
    assert report["status"] == "failed", report
    assert report["states"]["A"]["state"] == "FAILED"
    assert report["states"]["A"]["reason"] == "worker_exit_1"
    # done() is recorded as `completed`: the failed worker is absent, so
    # the dependent never became ready and is fail-closed BLOCKED
    assert report["completed"] == [], report["completed"]
    assert report["states"]["C"]["state"] == "BLOCKED"
    assert "dependency_not_finished:A" in report["states"]["C"]["reason"]
    assert ran == ["A"], ran  # C's worker never executed
    assert report["evidence"] == {}, report["evidence"]


# ---- 11.3.9: evidence must bind to the actual verified tree ---------------

def test_evidence_tree_identity_binding(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [_worker("A", writes=["tests/ev.py"],
                       cmd=_write_cmd("tests/ev.py"))]
    sched = orchestrator.GovernedScheduler(repo, workers, task_id="t-evidence",
                                           keep_worktrees=True)
    report = sched.run()
    _assert_ok(report)
    evi = Path(sched.evidence_paths["A"])
    worktree = Path(report["worktrees"]["A"])
    original = evi.read_text(encoding="utf-8")
    try:
        # baseline: freshly sealed evidence verifies against the live tree
        payload = orchestrator.verify_worker_evidence(evi,
                                                      worktree=worktree)
        assert payload["target_tree_sha"] == payload["tree_hash"]
        assert payload["authorized_to_ship"] is False

        # (a) tampered field without resealing -> digest mismatch
        obj = json.loads(original)
        obj["observed_scope"] = ["evil.py"]
        evi.write_text(json.dumps(obj, indent=2, sort_keys=True),
                       encoding="utf-8")
        with pytest.raises(orchestrator.OrchestratorError,
                           match="digest mismatch"):
            orchestrator.verify_worker_evidence(evi, worktree=worktree)

        # (b) VALID digest but wrong tree identity -> live re-binding fails
        obj = json.loads(original)
        obj["tree_hash"] = "0" * 40
        obj["target_tree_sha"] = "0" * 40
        evi.write_text(json.dumps(evidence.seal(obj), indent=2,
                                  sort_keys=True), encoding="utf-8")
        with pytest.raises(orchestrator.OrchestratorError,
                           match="tree identity mismatch"):
            orchestrator.verify_worker_evidence(evi, worktree=worktree)

        # (c) missing tree identity field -> invalid regardless of digest
        obj = json.loads(original)
        obj.pop("target_tree_sha")
        evi.write_text(json.dumps(evidence.seal(obj), indent=2,
                                  sort_keys=True), encoding="utf-8")
        with pytest.raises(orchestrator.OrchestratorError,
                           match="missing fields"):
            orchestrator.verify_worker_evidence(evi)

        # (d) worker evidence claiming ship authorization -> rejected
        obj = json.loads(original)
        obj["authorized_to_ship"] = True
        evi.write_text(json.dumps(evidence.seal(obj), indent=2,
                                  sort_keys=True), encoding="utf-8")
        with pytest.raises(orchestrator.OrchestratorError,
                           match="authorized_to_ship"):
            orchestrator.verify_worker_evidence(evi)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force",
                        str(worktree)], cwd=repo, capture_output=True,
                       timeout=60)
        subprocess.run(["git", "worktree", "prune"], cwd=repo,
                       capture_output=True, timeout=60)


# ---- 11.3.10: cycle detection fails closed --------------------------------

def test_cycle_fails_closed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [_worker("A", deps=("B",)), _worker("B", deps=("A",))]
    sched = orchestrator.GovernedScheduler(repo, workers, task_id="t-cycle")
    report = sched.run()
    assert report["status"] == "failed"
    assert report["reason"] == "cycle"
    assert set(report["cycle"]) == {"A", "B"}
    assert report["states"]["A"]["state"] == "FAILED"
    assert report["states"]["A"]["reason"] == "cycle"
    assert report["completed"] == []
    # nothing was created for a cyclic graph
    assert report["worktrees"] == {}
    assert not (repo.parent / (repo.name + ".worktrees")).exists()


# ---- §5: post-execution scope validation fails verification ---------------

def test_scope_violation_fails_verification(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [_worker("A", declared=("tests/",),
                       cmd=_write_cmd("pkg/leaf.py"))]
    sched = orchestrator.GovernedScheduler(repo, workers, task_id="t-scope")
    report = sched.run()
    assert report["status"] == "failed", report
    entry = report["states"]["A"]
    assert entry["state"] == "INVALID_EVIDENCE", entry
    assert entry["reason"].startswith("scope_violation"), entry
    assert report["completed"] == []
    assert report["evidence"] == {}
    # the out-of-scope change never left the (removed) worktree
    assert _git(repo, "status", "--porcelain") == ""
    assert not (repo / "pkg" / "leaf.py").read_text().count(
        "written by fixture worker")


# ---- §3: unknown declared scope blocks dispatch (UNKNOWN != SAFE) ---------

def test_unknown_declared_scope_blocks_dispatch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ran: list[str] = []

    def execute(worker: dict, worktree: Path):
        ran.append(worker["id"])
        return 0

    workers = [_worker("A", declared=None), _worker("C", deps=("A",))]
    sched = orchestrator.GovernedScheduler(repo, workers, task_id="t-noscope",
                                           execute=execute)
    report = sched.run()
    assert report["status"] == "failed"
    assert report["states"]["A"]["state"] == "BLOCKED"
    assert report["states"]["A"]["reason"] == "unknown_declared_scope"
    assert report["states"]["C"]["state"] == "BLOCKED"
    assert "dependency_not_finished:A" in report["states"]["C"]["reason"]
    assert ran == [], ran
    assert report["completed"] == []


# ---- 11.4: real worktree lifecycle ----------------------------------------

def test_worktree_lifecycle_real_git(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base_status = _git(repo, "status", "--porcelain")
    ok_content = (repo / "tests" / "test_ok.py").read_text(encoding="utf-8")
    workers = [_worker("A", writes=["tests/wt_probe.py"],
                       cmd=_write_cmd("tests/wt_probe.py"))]
    sched = orchestrator.GovernedScheduler(repo, workers, task_id="t-wt")
    report = sched.run()
    _assert_ok(report)

    # create + isolate: the worker's change exists ONLY in its worktree
    assert _git(repo, "status", "--porcelain") == base_status
    assert not (repo / "tests" / "wt_probe.py").exists()
    assert (repo / "tests" / "test_ok.py").read_text(
        encoding="utf-8") == ok_content

    # execute + diff/tree + evidence collection
    payload = orchestrator.verify_worker_evidence(
        Path(report["evidence"]["A"]))  # worktree already gone: digest mode
    assert payload["observed_scope"] == ["tests/wt_probe.py"]
    assert payload["declared_scope"] == ["tests/"]
    assert "tests/wt_probe.py" in payload["diff"]
    assert payload["exit_status"] == 0
    assert payload["authorized_to_ship"] is False
    # the sealed tree is a NEW tree produced inside the worktree
    assert payload["target_tree_sha"] != report["base_tree"]
    assert all(check["status"] in ("passed", "skipped")
               for check in payload["checks"])
    assert any(check["name"] == "pytest" and check["status"] == "passed"
               for check in payload["checks"])

    # cleanup: worktree removed, registration pruned, report honest
    wt_path = Path(report["worktrees"]["A"])
    assert not wt_path.exists()
    registrations = [line for line in
                     _git(repo, "worktree", "list", "--porcelain")
                     .splitlines() if line.startswith("worktree ")]
    assert len(registrations) == 1, registrations  # only the main tree
    assert report["cleanup_errors"] == [], report["cleanup_errors"]
    assert not (repo.parent / (repo.name + ".worktrees")).exists()


# ---- 11.5: integration A + B -> C -----------------------------------------

def test_integration_ab_c_evidence_ordering(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    barrier = _Barrier(2)
    events: list[str] = []
    box: dict = {}

    def execute(worker: dict, worktree: Path):
        wid = worker["id"]
        if wid in ("A", "B"):
            events.append(f"{wid}_run")
            if not barrier.arrive(wid):
                return 1
            return orchestrator.default_execute(worker, worktree)
        # C: both parents must be DONE and their evidence sealed on disk
        for dep in ("A", "B"):
            assert box["sched"].states[dep]["state"] == "DONE", \
                box["sched"].states
            evi = box["sched"].evidence_paths.get(dep)
            assert evi and Path(evi).is_file(), box["sched"].evidence_paths
        events.append("C_run")
        return orchestrator.default_execute(worker, worktree)

    workers = [
        _worker("A", writes=["tests/worker_a.py"],
                cmd=_write_cmd("tests/worker_a.py")),
        _worker("B", writes=["tests/worker_b.py"],
                cmd=_write_cmd("tests/worker_b.py")),
        _worker("C", deps=("A", "B"), writes=["tests/worker_c.py"],
                cmd=_write_cmd("tests/worker_c.py")),
    ]
    sched = orchestrator.GovernedScheduler(repo, workers, task_id="t-abc",
                                           execute=execute)
    box["sched"] = sched
    report = sched.run()
    _assert_ok(report)
    # A and B really overlapped (barrier) and neither was deferred
    assert set(barrier.started) == {"A", "B"}, barrier.started
    assert report["deferral_events"] == [], report["deferral_events"]
    # C waited for both complete evidence lifecycles
    assert events[2] == "C_run" and set(events[:2]) == {"A_run", "B_run"}, \
        events
    assert report["completed"][-1] == "C"
    assert set(report["completed"]) == {"A", "B", "C"}
    # integration result exists only inside worktrees, never in main tree
    assert _git(repo, "status", "--porcelain") == ""
    assert not (repo / "tests" / "worker_a.py").exists()


# ---- CLI: governed control.py delegation + fail-closed bad spec -----------

def test_control_cli_delegation_and_bad_spec(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    spec = {
        "task_id": "cli",
        "workers": [_worker("A", writes=["tests/cli_probe.py"],
                            cmd=_write_cmd("tests/cli_probe.py"))],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    # governed entry point: transport gate + delegation through control.py
    proc = subprocess.run(
        [PY, str(CONTROL), "--transport", "local", "--root", str(repo),
         "orchestrator", "--spec", str(spec_path)],
        cwd=repo, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["status"] == "ok", report.get("reason")
    assert report["states"]["A"]["state"] == "DONE"
    assert Path(report["evidence"]["A"]).is_file()
    assert _git(repo, "status", "--porcelain") == ""

    # missing transport is already gate-tested elsewhere; here: missing
    # spec file fails closed with a clean error (no traceback, no dispatch)
    proc = subprocess.run(
        [PY, str(ORCHESTRATOR), "--root", str(repo), "run",
         "--spec", str(tmp_path / "missing.json")],
        cwd=repo, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    assert "ORCHESTRATOR ERROR" in proc.stderr
    assert "Traceback" not in proc.stderr

    # structurally invalid graph fails closed before any dispatch
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"task_id": "b", "workers": [
        {"id": "A", "deps": ["nope"], "cmd": [PY, "-c", "pass"]}]}),
        encoding="utf-8")
    proc = subprocess.run(
        [PY, str(ORCHESTRATOR), "--root", str(repo), "run",
         "--spec", str(bad)],
        cwd=repo, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    assert "unknown dependency" in proc.stderr
    assert not (repo.parent / (repo.name + ".worktrees")).exists()
