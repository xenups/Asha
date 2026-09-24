"""Phase 2.4 -- controlled multi-agent execution validation.

Every test drives the SAME real runtime entrypoint Asha ships:
`asha.scheduler.GovernedScheduler.run()` -- admission -> lifecycle FSM
-> graph decide -> DispatchIntent -> governance drop/authorize ->
physical worktree/execute -> reconcile -> evidence report. No parallel
orchestration layer, no duplicated scheduler/graph logic, no sleeps:
all synchronization is state-based (barrier-free, deterministic
interleavings asserted as exact event sequences).

Map to the task steps:

    STEP 3/4/5  test_governed_order_locality_and_independence
    STEP 6/9    test_stale_intent_guards_physical_boundary_multiagent
    STEP 7      test_orphaned_target_blocks_consumer_with_peer_running
    STEP 8      test_dynamic_cycle_fail_closed_multiagent_runtime
    STEP 10     test_evidence_reconstructs_terminal_workers
"""

import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

PY = sys.executable

GITIGNORE = (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
             ".mypy_cache/\n.ruff_cache/\n")

BASE_CORE = {
    ".gitignore": GITIGNORE,
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
}


# -- fixture plumbing (self-contained; mirrors the established fixture) ------

def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    files = dict(BASE_CORE)
    files.update(extra or {})
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)
    return proc.stdout.strip()


def _w(wid: str, *, declared: Sequence[str] = ("tests/",),
       deps: Sequence[str] = (), reads: Sequence[str] = (),
       writes: Sequence[str] = (), cmd: list[str] | None = None) -> dict:
    return {"id": wid, "deps": list(deps),
            "declared_scope": list(declared),
            "reads": list(reads), "writes": list(writes),
            "cmd": cmd or [PY, "-c", "pass"]}


def _py(lines: list[str]) -> list[str]:
    return [PY, "-c", "; ".join(lines)]


# -- physical boundary probes (OS/filesystem observation only) ---------------

def _spy_processes(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Count every OS process boundary the engine can cross."""
    journal: list[tuple] = []

    def _argv_of(args: tuple, kwargs: dict) -> tuple:
        argv = args[0] if args else kwargs.get("args")
        if argv is None:
            return ()
        if isinstance(argv, (str, bytes)):
            return (argv,)
        return tuple(str(part) for part in argv)

    for name in ("run", "call", "check_call", "check_output", "Popen"):
        real = getattr(subprocess, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            journal.append((_name, _argv_of(args, kwargs)))
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, spy)
    return journal


def _worktree_adds(journal: list[tuple]) -> list[tuple]:
    return [entry for entry in journal
            if entry[0] == "run"
            and "worktree" in entry[1] and "add" in entry[1]]


def _fs_snapshot(*roots: Path) -> list[tuple]:
    """Filesystem truth (sorted, .git internals excluded -- worker
    mutations never land there and reads must stay side-effect free)."""
    snap: list[tuple] = []
    for root in roots:
        if not root.exists():
            snap.append((str(root), "<absent>"))
            continue
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if rel == ".git" or rel.startswith(".git/"):
                continue
            snap.append((str(root), rel, path.is_dir()))
    return snap


# ------------------------------------------------------------ fixtures


def _pipeline_workers():
    """STEP 3 topology: a -> b, d -> b, c independent.

    Dependency representation is Asha's EXISTING fixture contract:
    declared `deps` (consumer lists its producer) plus declared reads --
    no second dependency model. Written files stay import-independent so
    no cross-worktree module resolution is involved.
    """
    return [
        _w("worker_a", declared=["pkg/feature.py"],
           writes=["pkg/feature.py"],
           cmd=_py(["from pathlib import Path",
                    ("Path('pkg/feature.py').write_text("
                     "'def provide() -> int:\\n    return 7\\n')")])),
        _w("worker_b", declared=["pkg/consumer.py"],
           writes=["pkg/consumer.py"], deps=["worker_a"],
           reads=["pkg/feature.py"],
           cmd=_py(["from pathlib import Path",
                    ("Path('pkg/consumer.py').write_text("
                     "'B_VALUE = 2\\n')")])),
        _w("worker_c", declared=["pkg/independent.py"],
           writes=["pkg/independent.py"],
           cmd=_py(["from pathlib import Path",
                    ("Path('pkg/independent.py').write_text("
                     "'C_VALUE = 3\\n')")])),
        _w("worker_d", declared=["pkg/downstream.py"],
           writes=["pkg/downstream.py"], deps=["worker_b"],
           reads=["pkg/consumer.py"],
           cmd=_py(["from pathlib import Path",
                    ("Path('pkg/downstream.py').write_text("
                     "'D_VALUE = 4\\n')")])),
    ]


def _trace(sched, monkeypatch, on_decide=None):
    """Observe the governance/physical boundary WITHOUT altering it.

    Records every `_decide` verdict (with graph generation + completed
    set), every physical `dispatcher.create` (worktree) with its
    generation, and every `execute()` entry with its completed set.
    `on_decide(wid, call_no)` runs BEFORE the real decision so a test
    may inject an adversarial repository change at a precise state.
    """
    events, calls = [], {}
    real_decide = sched._decide
    real_create = sched.dispatcher.create
    real_execute = sched.execute

    def decide(worker):
        wid = str(worker["id"])
        calls[wid] = calls.get(wid, 0) + 1
        if on_decide is not None:
            on_decide(wid, calls[wid])
        verdict = real_decide(worker)
        events.append(("decide", wid, verdict[0], verdict[1],
                       sched.graph.generation, tuple(sched.completed)))
        return verdict

    def create(wid):
        events.append(("create", wid, sched.graph.generation))
        return real_create(wid)

    def execute(worker, path):
        events.append(("execute", str(worker["id"]),
                       sched.graph.generation, tuple(sched.completed)))
        return real_execute(worker, path)

    monkeypatch.setattr(sched, "_decide", decide)
    monkeypatch.setattr(sched.dispatcher, "create", create)
    monkeypatch.setattr(sched, "execute", execute)
    return events, calls


def _transitions(sched, monkeypatch):
    """Lifecycle record at the state-machine boundary (STEP 4/7)."""
    seq, real_set = [], sched._set

    def set_(wid, state, reason=None):
        seq.append((wid, state, reason))
        return real_set(wid, state, reason)

    monkeypatch.setattr(sched, "_set", set_)
    return seq


def _decides(events):
    return [e for e in events if e[0] == "decide"]


def _creates(events):
    return [e for e in events if e[0] == "create"]


def _executes(events):
    return [e for e in events if e[0] == "execute"]


def _states(seq, wid):
    return [s for w, s, _ in seq if w == wid]


# ------------------------------------------------------- STEP 3/4/5 tests


def test_governed_order_locality_and_independence(tmp_path, monkeypatch):
    """A -> reconcile -> B (never reversed); C stays independent."""
    from asha.scheduler import GovernedScheduler

    repo = _repo(tmp_path, {})
    sched = GovernedScheduler(repo, _pipeline_workers(),
                              task_id="p24-pipeline")
    events, _ = _trace(sched, monkeypatch)
    seq = _transitions(sched, monkeypatch)

    report = sched.run()

    # STEP 4: governed admission order is exact, not timing-based.
    decide_order = [e[1] for e in _decides(events)]
    assert decide_order == ["worker_a", "worker_c",
                            "worker_b", "worker_d"]
    for e in _decides(events):
        if e[1] == "worker_b" and e[2] == "dispatch":
            assert "worker_a" in e[5]      # B authorized only with A done
        if e[1] == "worker_d" and e[2] == "dispatch":
            assert "worker_b" in e[5]      # D authorized only with B done
    exec_order = [e[1] for e in _executes(events)]
    assert (exec_order.index("worker_a") < exec_order.index("worker_b")
            < exec_order.index("worker_d"))
    for e in _executes(events):            # causality INSIDE execution
        if e[1] == "worker_b":
            assert "worker_a" in e[3] and "worker_d" not in e[3][:1]
        if e[1] == "worker_d":
            assert "worker_b" in e[3]

    # STEP 5: unrelated C executed while A/B reconciled, and nothing
    # about C leaked into the dependent subgraph.
    assert "worker_c" in report["completed"]
    assert sched.deferral_events == []      # no spurious invalidation
    assert _states(seq, "worker_b") == ["RUNNING", "DONE"]
    assert _states(seq, "worker_d") == ["RUNNING", "DONE"]
    assert _states(seq, "worker_a") == ["RUNNING", "DONE"]
    # exact topology: C connected to nobody, no extra edge ever appears
    assert {k: set(v) for k, v in
            sched.worker_graph["edges"].items()} == {
        "worker_a": set(),
        "worker_b": {"worker_a"},
        "worker_c": set(),
        "worker_d": {"worker_b"},
    }
    assert report["status"] == "ok"
    assert set(report["completed"]) == {
        "worker_a", "worker_b", "worker_c", "worker_d"}
    assert report["graph"]["failures"] == []
    json.dumps({"report": report, "log": sched.reconcile_log})  # valid


# -------------------------------------------------------- STEP 6/9 tests


def test_stale_intent_guards_physical_boundary_multiagent(
        tmp_path, monkeypatch):
    """Repo change at N+1: B's N-intent never reaches the OS boundary;
    B executes only via the intent re-derived from the current gen."""
    from asha.scheduler import GovernedScheduler

    repo = _repo(tmp_path, {})
    sched = GovernedScheduler(repo, _pipeline_workers(),
                              task_id="p24-stale")
    journal = _spy_processes(monkeypatch)
    memo = {"idx0": None, "idx1": None, "snap0": None, "snap1": None,
            "inject_gen": None}

    def on_decide(wid, count):
        if wid == "worker_b" and count == 1:
            # repository change: Generation N -> N+1
            assert sched._reconcile({
                "pkg/marker2.py": "import sys\n\nM2 = sys.version\n"})
            memo["inject_gen"] = sched.graph.generation
            memo["snap0"] = _fs_snapshot(repo)
            memo["idx0"] = len(journal)
        elif wid == "worker_b" and count == 2:
            # B's re-evaluation AFTER the stale drop
            memo["snap1"] = _fs_snapshot(repo)
            memo["idx1"] = len(journal)

    events, _ = _trace(sched, monkeypatch, on_decide=on_decide)
    report = sched.run()

    # STEP 6: rejection evidence at the runtime contract
    stale = [e for e in sched.stale_intents if e["worker"] == "worker_b"]
    assert len(stale) == 1
    assert stale[0]["reason"] == "STALE_GRAPH_GENERATION"
    assert stale[0]["intent_generation"] < stale[0]["current_generation"]
    assert memo["inject_gen"] == stale[0]["current_generation"]

    # physical boundary: the stale window spawned nothing for B
    window = journal[memo["idx0"]:memo["idx1"]]
    assert not any("worktree" in e[1] and "add" in e[1] for e in window)
    assert not any("worker_b" in " ".join(e[1]) for e in window)
    assert memo["snap0"] == memo["snap1"]      # main tree untouched
    b_creates = [e for e in _creates(events) if e[1] == "worker_b"]
    assert len(b_creates) == 1
    # B executed ONLY through the newly derived intent (current gen)
    assert b_creates[0][2] == stale[0]["current_generation"]
    assert b_creates[0][2] > stale[0]["intent_generation"]
    assert len(_worktree_adds(journal)) == 4    # one per worker, all valid
    b_exec = [e for e in _executes(events) if e[1] == "worker_b"]
    assert len(b_exec) == 1 and "worker_a" in b_exec[0][3]

    # STEP 9: proposal != authorization; authorization == governance
    # (observable form): every physical create/execute is preceded by a
    # dispatch verdict for that same worker, and the rejected proposal
    # never became a create.
    dispatch_seen = set()
    for e in events:
        if e[0] == "decide" and e[2] == "dispatch":
            dispatch_seen.add(e[1])
        elif e[0] in ("create", "execute"):
            assert e[1] in dispatch_seen        # authorization precedes
    assert stale[0]["intent_generation"] not in {
        e[2] for e in _creates(events)}         # dead intent never created

    assert report["status"] == "ok"
    assert set(report["completed"]) == {
        "worker_a", "worker_b", "worker_c", "worker_d"}
    assert sched.state_of("worker_b") == "DONE"


# --------------------------------------------------------- STEP 7 tests


def test_orphaned_target_blocks_consumer_with_peer_running(
        tmp_path, monkeypatch):
    """helper.py::calc vanishes after A publishes it and while unrelated
    C is still running: B must be evicted, never dispatched."""
    from asha.scheduler import GovernedScheduler

    workers = [
        _w("worker_a", declared=["pkg/helper.py"],
           writes=["pkg/helper.py"],
           cmd=_py(["from pathlib import Path",
                    ("Path('pkg/helper.py').write_text("
                     "'import os\\n\\n\\ndef calc() -> int:\\n"
                     "    return len(os.getcwd())\\n')")])),
        _w("worker_b", declared=["pkg/consumer.py"],
           writes=["pkg/consumer.py"], deps=["worker_a"],
           reads=["pkg/helper.py"]),
        _w("worker_c", declared=["pkg/independent.py"],
           writes=["pkg/independent.py"],
           cmd=_py(["from pathlib import Path",
                    ("Path('pkg/independent.py').write_text("
                     "'C_VALUE = 3\\n')")])),
    ]
    repo = _repo(tmp_path, {})
    sched = GovernedScheduler(repo, workers, task_id="p24-orphan")
    memo = {"injected": False}
    verdicts = []

    def on_decide(wid, count):
        if (wid == "worker_b" and not memo["injected"]
                and "pkg/helper.py" in sched.graph.nodes
                and "worker_a" in sched.completed):
            # adversarial reconcile: delete the consumed target
            assert sched._reconcile({"pkg/helper.py": None})
            assert "pkg/helper.py" not in sched.graph.nodes
            memo["injected"] = True

    events, _ = _trace(sched, monkeypatch, on_decide=on_decide)
    seq = _transitions(sched, monkeypatch)
    real_decide = sched._decide

    def decide_cap(worker):
        verdict = real_decide(worker)
        if worker["id"] == "worker_b":
            verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(sched, "_decide", decide_cap)
    report = sched.run()

    assert memo["injected"] is True
    # B's ONE decision is the eviction; dispatch never appears
    assert verdicts == [("block",
                         "orphaned_dependency:pkg/helper.py")]
    assert _states(seq, "worker_b") == ["BLOCKED"]
    assert "RUNNING" not in _states(seq, "worker_b")
    assert not [e for e in _creates(events) if e[1] == "worker_b"]
    assert not [e for e in _executes(events) if e[1] == "worker_b"]
    # peer C finished independently; A's publication stands
    assert sched.state_of("worker_c") == "DONE"
    assert sched.state_of("worker_a") == "DONE"
    assert report["status"] == "failed"
    assert set(report["completed"]) == {"worker_a", "worker_c"}
    assert report["states"]["worker_b"]["state"] == "BLOCKED"
    assert report["states"]["worker_b"]["reason"].startswith(
        "orphaned_dependency:")


# --------------------------------------------------------- STEP 8 tests


def test_dynamic_cycle_fail_closed_multiagent_runtime(
        tmp_path, monkeypatch):
    """worker_c's edits introduce A->B and B->A worker edges (file
    graph stays acyclic): both blocked, D held by dependency, zero
    unauthorized execution, structured cycle evidence."""
    from asha.scheduler import GovernedScheduler

    touched = ("pkg/agent_a/__init__.py", "pkg/agent_a/x.py",
               "pkg/agent_a/w.py", "pkg/agent_b/__init__.py",
               "pkg/agent_b/y.py", "pkg/agent_b/z.py")
    toucher = _py(
        ["from pathlib import Path"]
        + [f"Path('{rel}').write_text("
           f"Path('{rel}').read_text() + '# touched\\n')"
           for rel in touched])
    workers = [
        _w("worker_a", declared=["pkg/agent_a/"], deps=["worker_c"],
           reads=["pkg/agent_b/y.py"],
           writes=["pkg/agent_a/x.py", "pkg/agent_a/w.py"]),
        _w("worker_b", declared=["pkg/agent_b/"], deps=["worker_c"],
           reads=["pkg/agent_a/w.py"],
           writes=["pkg/agent_b/y.py", "pkg/agent_b/z.py"]),
        _w("worker_c", declared=["pkg/"], reads=[], writes=[],
           cmd=toucher),
        _w("worker_d", declared=["pkg/agent_d/"], deps=["worker_b"],
           reads=["pkg/agent_b/y.py"],
           writes=["pkg/agent_d/downstream.py"]),
    ]
    repo = _repo(tmp_path, {
        "pkg/agent_a/__init__.py": "",
        "pkg/agent_b/__init__.py": "",
        "pkg/agent_d/__init__.py": "",
        "pkg/agent_a/x.py":
            "from pkg.agent_b import y\n\nX_VALUE = y.Y_VALUE\n",
        "pkg/agent_b/y.py": "Y_VALUE = 1\n",
        "pkg/agent_b/z.py":
            "from pkg.agent_a import w\n\nZ_VALUE = w.W_VALUE\n",
        "pkg/agent_a/w.py": "W_VALUE = 1\n",
    })
    sched = GovernedScheduler(repo, workers, task_id="p24-cycle")
    events, _ = _trace(sched, monkeypatch)

    report = sched.run()

    # deterministic fail-closed + evidence content
    cycle_entries = [e for e in sched.reconcile_log
                     if not e["ok"]
                     and str(e["reason"]).startswith("worker_cycle:")]
    assert cycle_entries
    entry = cycle_entries[0]
    members = set(entry["reason"].split(":", 1)[1].split(","))
    assert members == {"worker_a", "worker_b"}
    assert entry["generation"] == report["graph"]["generation"]
    assert sched.worker_cycle_members == frozenset({"worker_a",
                                                    "worker_b"})
    assert report["states"]["worker_a"]["state"] == "BLOCKED"
    assert report["states"]["worker_a"]["reason"] == \
        "worker_cycle_member"
    assert report["states"]["worker_b"]["state"] == "BLOCKED"
    assert report["states"]["worker_b"]["reason"] == "worker_cycle_member"
    # D never became eligible: held fail-closed on its dependency
    assert report["states"]["worker_d"]["state"] == "BLOCKED"
    assert report["states"]["worker_d"]["reason"] == \
        "dependency_not_finished:worker_b"

    # no partial execution escapes the governance boundary
    assert [e[1] for e in _creates(events)] == ["worker_c"]
    assert [e[1] for e in _executes(events)] == ["worker_c"]
    unauthorized = [e for e in _decides(events)
                    if e[1] != "worker_c" and e[2] == "dispatch"]
    assert unauthorized == []          # no worker proposal was authorized
    assert sched.state_of("worker_c") == "DONE"

    # `dispatch_authorized` does NOT exist in Asha's evidence schema
    # (verified: repo-wide grep finds no such field) -- record that the
    # conditional assertion evaluated against the real schema.
    assert "dispatch_authorized" not in report
    blob = json.dumps({"report": report, "log": sched.reconcile_log},
                      sort_keys=True)
    assert "worker_cycle:worker_a,worker_b" in blob
    assert json.loads(blob)["report"]["states"]["worker_a"][
        "state"] == "BLOCKED"


# -------------------------------------------------------- STEP 10 tests


def _assert_evidence(report, log, wids, created, executed):
    """Existing evidence schema must reconstruct every terminal worker."""
    blob = json.dumps({"report": report, "log": log}, sort_keys=True)
    assert json.loads(blob)              # serialization valid
    gens = [e["generation"] for e in log]
    assert gens == sorted(gens)          # generation track reconstructable
    ok_gens = [e["generation"] for e in log if e["ok"]]
    assert ok_gens and ok_gens[-1] == report["graph"]["generation"]
    assert set(report["states"]) == set(wids)   # identity of every worker
    for wid in wids:
        entry = report["states"][wid]
        state, reason = entry["state"], entry["reason"]
        assert state in ("DONE", "FAILED", "BLOCKED")
        if wid in report["completed"]:
            # execution result + authorization happened
            assert state == "DONE" and reason is None
            assert wid in created and wid in executed
        else:
            # rejection/block reason where execution did NOT occur
            assert state in ("BLOCKED", "FAILED")
            assert reason
            assert wid not in executed
    # dependency decisions live in deferral events / terminal reasons
    for event in report["deferral_events"]:
        assert {"worker", "reason"} <= set(event)
        assert event["worker"] in wids


def test_evidence_reconstructs_terminal_workers(tmp_path, monkeypatch):
    from asha.scheduler import GovernedScheduler

    # scenario 1: full pipeline (all executed)
    (tmp_path / "happy").mkdir()
    repo = _repo(tmp_path / "happy", {})
    sched = GovernedScheduler(repo, _pipeline_workers(),
                              task_id="p24-ev-ok")
    events1, _ = _trace(sched, monkeypatch)
    report1 = sched.run()
    assert report1["status"] == "ok"
    created1 = [e[1] for e in _creates(events1)]
    executed1 = [e[1] for e in _executes(events1)]
    _assert_evidence(report1, sched.reconcile_log,
                     ["worker_a", "worker_b", "worker_c", "worker_d"],
                     created1, executed1)

    # scenario 2: orphan eviction (one terminal BLOCKED, no execution)
    (tmp_path / "orphan").mkdir()
    repo2 = _repo(tmp_path / "orphan", {})
    workers2 = [
        _w("worker_a", declared=["pkg/helper.py"],
           writes=["pkg/helper.py"],
           cmd=_py(["from pathlib import Path",
                    ("Path('pkg/helper.py').write_text("
                     "'import os\\n\\n\\ndef calc() -> int:\\n"
                     "    return len(os.getcwd())\\n')")])),
        _w("worker_b", declared=["pkg/consumer.py"],
           writes=["pkg/consumer.py"], deps=["worker_a"],
           reads=["pkg/helper.py"]),
    ]
    sched2 = GovernedScheduler(repo2, workers2, task_id="p24-ev-orphan")
    memo = {"injected": False}

    def on_decide(wid, count):
        if (wid == "worker_b" and not memo["injected"]
                and "pkg/helper.py" in sched2.graph.nodes
                and "worker_a" in sched2.completed):
            assert sched2._reconcile({"pkg/helper.py": None})
            memo["injected"] = True

    events2, _ = _trace(sched2, monkeypatch, on_decide=on_decide)
    report2 = sched2.run()
    assert memo["injected"] is True
    created2 = [e[1] for e in _creates(events2)]
    executed2 = [e[1] for e in _executes(events2)]
    _assert_evidence(report2, sched2.reconcile_log,
                     ["worker_a", "worker_b"], created2, executed2)
    # the non-execution reason itself identifies the orphaned target
    assert report2["states"]["worker_b"]["reason"] == (
        "orphaned_dependency:pkg/helper.py")
