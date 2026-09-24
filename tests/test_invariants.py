"""Phase 2.3 -- deterministic adversarial invariants (metamorphic suite).

Seven invariant families, each asserting the LOWEST physical or semantic
boundary the contract names (task 2.3 spec):

    1. test_stale_intent_blocks_every_physical_boundary
       DispatchIntent stamped at generation N loses to a real
       reconciliation bump -> rejection reason STALE_GRAPH_GENERATION
       with ZERO subprocess spawns, ZERO worktree creations, ZERO
       filesystem mutations and ZERO entrypoint executions in the
       reject window (spies observe OS boundaries, never decision
       functions; the race is injected at the test layer only).
    2. test_stale_intent_never_resurrects_across_generations
       Dropped intents at N and N+1 stay dropped while the scheduler
       advances to N+2; the worker dispatches exactly once, against the
       LIVE generation only.
    3. test_generation_monotonic_across_coalesce_failures_rollback
       graph revision -> strictly +1; failures/rollback/ambiguity hold
       the generation; no observation ever decreases it.
    4. test_locality_of_invalidation_disjoint_mutation
       A mutation outside worker B's existing-closure leaves B's state,
       edges, frontier membership and decision verdict semantically
       identical while the generation advances.
    5. test_frontier_determinism_and_idempotence
       Repeated frontier evaluation over an unchanged repository yields
       identical semantic states and exact rankings.
    6. test_target_eviction_severs_ready_worker
       Deleting a published read target evicts the dependency-ready
       worker straight into BLOCKED (existing state label) -- it never
       dispatches against an orphaned dependency.
    7. test_dynamic_worker_cycle_fails_closed_with_structured_evidence
       Worker edges derived dynamically from worker output form a cycle
       A<->B: candidate rejected atomically, both members BLOCKED, and
       the evidence log carries machine-readable generation/members/
       status.

Graph semantics are Asha's own (worker_graph.derive edge direction,
graph_state closure); no state labels are invented; all injection uses
test-layer hooks (dependency injection), never production flags.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from asha import graph_state, worker_graph
from asha.scheduler import GovernedScheduler, default_execute

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


def _wt_root(repo: Path) -> Path:
    return repo.parent / (repo.name + ".worktrees")


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


# -- semantic helpers --------------------------------------------------------

def _frontier(sched: GovernedScheduler) -> dict:
    """Semantic frontier: published states + readiness over the CURRENT
    WorkerGraph (no object identity, task constraint 10)."""
    combined = {worker["id"]:
                set(sched.worker_graph["edges"].get(worker["id"]) or set())
                for worker in sched.workers}
    return {"states": {wid: dict(entry)
                       for wid, entry in sched.states.items()},
            "ready": list(worker_graph.build_sorter(combined).get_ready())}


def _b_snapshot(sched: GovernedScheduler) -> dict:
    return {
        "state": dict(sched.states["B"]),
        "edges": set(sched.worker_graph["edges"].get("B") or set()),
        "frontier": list(_frontier(sched)["ready"]),
        "decide": sched._decide(sched.by_id["B"]),
    }


def _reverse_closure(state: graph_state.GraphState,
                     start: set[str]) -> set[str]:
    """Asha's existing closure semantics: reverse-edge reachability."""
    seen: set[str] = set()
    queue = sorted(start)
    while queue:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.add(node)
        queue.extend(sorted(state.reverse_edges.get(node, ())))
    return seen


# ---------------------------------------------------------------------------
# Invariant 1 -- physical zero-spawn on stale generation
# ---------------------------------------------------------------------------

def test_stale_intent_blocks_every_physical_boundary(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Generation-N intent raced by a real reconciliation bump is
    rejected at the PHYSICAL boundary: 0 spawns, 0 worktrees, 0 fs
    mutations, 0 entrypoint executions inside the reject window."""
    repo = _repo(tmp_path, {"pkg/w.py": "VALUE = 1\n"})
    workers = [_w("W", declared=["pkg/w.py"], writes=["pkg/w.py"],
                  cmd=_py(["from pathlib import Path",
                           "Path('pkg/w.py').write_text('VALUE = 2\\n')"]))]
    sched = GovernedScheduler(repo, workers, task_id="p23-zero-spawn")

    journal = _spy_processes(monkeypatch)      # installed AFTER fixture git
    executed: list[str] = []
    real_execute = sched.execute

    def track_execute(worker, path):
        executed.append(str(worker["id"]))
        return real_execute(worker, path)

    monkeypatch.setattr(sched, "execute", track_execute)

    real_decide = sched._decide
    memo: dict = {"injected": False, "snapshot": None, "asserted": False}

    def decide_hook(worker):
        if not memo["injected"]:
            # Race injection (test layer): a reconciliation publication
            # lands between intent stamping and the dispatch attempt.
            memo["injected"] = True
            bumped = sched._reconcile(
                {"pkg/marker.py": "import sys\n\nM = sys.version\n"})
            assert bumped is True
            assert sched.graph.generation == 1
            memo["snapshot"] = _fs_snapshot(repo, _wt_root(repo))
            return real_decide(worker)
        if not memo["asserted"] and sched.stale_intents_dropped >= 1:
            memo["asserted"] = True
            # Lowest-boundary assertions, at the moment the scheduler
            # has just rejected the stale intent (call 2 = re-eval).
            entry = sched.stale_intents[-1]
            assert entry["reason"] == graph_state.STALE_GRAPH_GENERATION
            assert entry["worker"] == "W"
            assert entry["intent_generation"] == 0
            assert entry["current_generation"] == 1
            assert journal == []                                  # spawns
            assert _worktree_adds(journal) == []                  # worktrees
            assert _fs_snapshot(repo, _wt_root(repo)) == memo["snapshot"]
            assert executed == []                                 # entrypoint
        return real_decide(worker)

    monkeypatch.setattr(sched, "_decide", decide_hook)
    report = sched.run()

    assert memo["asserted"] is True, "stale rejection never exercised"
    assert report["status"] == "ok"
    assert report["graph"]["stale_intents_dropped"] == 1
    # exactly ONE physical dispatch happened -- AFTER the reject window
    assert len(_worktree_adds(journal)) == 1
    assert executed == ["W"]
    assert sched.state_of("W") == "DONE"


# ---------------------------------------------------------------------------
# Invariant 2 -- no stale intent resurrection
# ---------------------------------------------------------------------------

def test_stale_intent_never_resurrects_across_generations(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Intents rejected at N and N+1 never re-enter the frontier: the
    worker dispatches exactly once, at the live generation N+2."""
    repo = _repo(tmp_path, {"pkg/w.py": "VALUE = 1\n"})
    workers = [_w("W", declared=["pkg/w.py"], writes=["pkg/w.py"],
                  cmd=_py(["from pathlib import Path",
                           "Path('pkg/w.py').write_text('VALUE = 3\\n')"]))]
    sched = GovernedScheduler(repo, workers, task_id="p23-no-resurrect")

    created_at: list[int] = []
    real_create = sched.dispatcher.create

    def create_hook(worker_id: str) -> Path:
        created_at.append(sched.graph.generation)
        return real_create(worker_id)

    monkeypatch.setattr(sched.dispatcher, "create", create_hook)

    executed: list[str] = []
    real_execute = sched.execute

    def track_execute(worker, path):
        executed.append(str(worker["id"]))
        return real_execute(worker, path)

    monkeypatch.setattr(sched, "execute", track_execute)

    real_decide = sched._decide
    injections = 0
    marker_contents = (
        "pkg/adv1.py", "import sys\n\nADV1 = sys.version\n",
    )
    marker_two = (
        "pkg/adv2.py", "import os\n\nADV2 = 1\n",
    )

    def decide_hook(worker):
        nonlocal injections
        if injections == 0:
            injections += 1
            assert sched._reconcile({marker_contents[0]: marker_contents[1]})
        elif injections == 1:
            injections += 1
            assert sched._reconcile({marker_two[0]: marker_two[1]})
        return real_decide(worker)

    monkeypatch.setattr(sched, "_decide", decide_hook)
    report = sched.run()

    assert report["status"] == "ok"
    # both stale intents rejected, each exactly once, never recycled
    assert sched.stale_intents_dropped == 2
    assert len(sched.stale_intents) == 2
    assert [entry["reason"] for entry in sched.stale_intents] == [
        graph_state.STALE_GRAPH_GENERATION] * 2
    assert [entry["intent_generation"] for entry in sched.stale_intents] \
        == [0, 1]
    assert [entry["current_generation"] for entry in sched.stale_intents] \
        == [1, 2]
    # dispatch happened only against the LIVE generation (N+2)
    assert created_at == [2]
    # 2 injection publications + the worker's OWN completion pass
    assert report["graph"]["generation"] == 3
    assert executed == ["W"]                  # one run total: no retries
    assert sched.state_of("W") == "DONE"
    # ledger stable after completion: nothing resurrected later
    assert sched.stale_intents_dropped == len(sched.stale_intents) == 2


# ---------------------------------------------------------------------------
# Invariant 3 -- generation monotonicity tied to graph revision
# ---------------------------------------------------------------------------

def test_generation_monotonic_across_coalesce_failures_rollback(
        tmp_path: Path) -> None:
    """Coalesced completions publish once (+1); ambiguity and file-cycle
    rollbacks hold the generation; the sequence never decreases."""
    repo = _repo(tmp_path, {"pkg/p1.py": "VALUE = 1\n",
                            "pkg/p2.py": "VALUE = 1\n"})
    workers = [
        _w("P1", declared=["pkg/"], writes=["pkg/p1.py"],
           cmd=_py(["from pathlib import Path",
                    "Path('pkg/p1.py').write_text('VALUE = 2\\n')"])),
        _w("P2", declared=["pkg/"], writes=["pkg/p2.py"],
           cmd=_py(["from pathlib import Path",
                    "Path('pkg/p2.py').write_text('VALUE = 2\\n')"])),
        _w("F", declared=["pkg/"], writes=["pkg/fx.py"],
           cmd=[PY, "-c", "import sys; sys.exit(1)"]),
    ]
    barrier = threading.Barrier(2)

    def execute(worker, path):
        result = default_execute(worker, path)
        if worker["id"] in ("P1", "P2"):
            barrier.wait(timeout=15)   # aligned completion -> one wave
        return result

    sched = GovernedScheduler(repo, workers, task_id="p23-monotonic",
                              execute=execute)
    report = sched.run()

    assert report["status"] == "failed"          # F fails closed
    assert sched.state_of("F") == "FAILED"
    assert sched.state_of("P1") == "DONE"
    assert sched.state_of("P2") == "DONE"
    ok_entries = [entry for entry in sched.reconcile_log if entry["ok"]]
    assert len(ok_entries) == 1                  # ONE pass, coalesced
    assert set(ok_entries[0]["files"]) == {"pkg/p1.py", "pkg/p2.py"}

    before = sched.graph.generation               # == 1 after the pass
    ambiguous = sched._reconcile_batch([
        ("pkg/duo.py", "D = 1\n", "P1", "tree-a"),
        ("pkg/duo.py", "D = 2\n", "P2", "tree-b")])
    assert ambiguous is False                    # refuse, never guess
    assert sched.graph.generation == before      # failure holds generation

    rolled_back = sched._reconcile(
        {"pkg/cy1.py": "import pkg.cy2\n\nC1 = 1\n",
         "pkg/cy2.py": "import pkg.cy1\n\nC2 = 1\n"})
    assert rolled_back is False                  # file-cycle fail-closed
    assert sched.graph.generation == before      # previous state retained
    failure_reasons = [entry["reason"] for entry in sched.reconcile_log
                       if not entry["ok"]]
    assert any(str(reason).startswith("cycle:") for reason in failure_reasons)

    advanced = sched._reconcile(
        {"pkg/fin.py": "import sys\n\nFIN = sys.version\n"})
    assert advanced is True
    assert sched.graph.generation == before + 1

    generations = [entry["generation"] for entry in sched.reconcile_log]
    assert generations == sorted(generations)      # NEVER decreases/resets
    ok_generations = [entry["generation"]
                      for entry in sched.reconcile_log if entry["ok"]]
    # every publication is exactly +1 over the previous one (revision tie)
    assert ok_generations == list(range(1, len(ok_generations) + 1))


# ---------------------------------------------------------------------------
# Invariant 4 -- locality of invalidation
# ---------------------------------------------------------------------------

def _locality_scheduler(tmp_path: Path) -> GovernedScheduler:
    repo = _repo(tmp_path)
    workers = [
        _w("A", declared=["pkg/module_a.py"],
           writes=["pkg/module_a.py"]),
        _w("B", declared=["pkg/module_b.py"], reads=["pkg/module_b.py"],
           writes=["pkg/consumer.py"]),
    ]
    sched = GovernedScheduler(repo, workers, task_id="p23-locality")
    seeded = sched._reconcile({
        "pkg/module_b.py": "import pkg.leaf\n\nVALUE = pkg.leaf.LEAF\n",
        "pkg/leaf.py": "LEAF = 1\n",
    })
    assert seeded is True and sched.graph.generation == 1
    return sched


def test_locality_of_invalidation_disjoint_mutation(tmp_path: Path) -> None:
    """mutate(unrelated) where unrelated is outside Closure(B): B's
    readiness state, published edges, frontier and verdict stay
    semantically identical while the graph revision advances."""
    sched = _locality_scheduler(tmp_path)
    closure_b = _reverse_closure(sched.graph, {"pkg/module_b.py"})
    assert "pkg/unrelated.py" not in closure_b   # disjoint by construction

    before = _b_snapshot(sched)
    generation_before = sched.graph.generation

    changed = sched._reconcile(
        {"pkg/unrelated.py": "import sys\n\nUNRELATED = sys.version\n"})
    assert changed is True
    assert sched.graph.generation == generation_before + 1  # real revision
    assert "pkg/unrelated.py" in sched.graph.nodes  # mutation landed

    after = _b_snapshot(sched)
    assert after == before     # strictly identical, semantically compared


# ---------------------------------------------------------------------------
# Invariant 5 -- determinism & idempotence of the frontier
# ---------------------------------------------------------------------------

def test_frontier_determinism_and_idempotence(tmp_path: Path) -> None:
    """Same state -> same projection, same exact rankings, same verdicts
    across repeated evaluations with NO repository state change."""
    sched = _locality_scheduler(tmp_path)
    workers = sched.workers

    first = worker_graph.derive(workers, sched.graph.edges,
                                completed=frozenset(sched.completed))
    second = worker_graph.derive(workers, sched.graph.edges,
                                 completed=frozenset(sched.completed))
    assert {wid: set(preds) for wid, preds in first.edges.items()} == \
        {wid: set(preds) for wid, preds in second.edges.items()}
    assert dict(first.declared) == dict(second.declared)
    assert first.derived == second.derived
    assert first.uncertain == second.uncertain

    sorter_a = worker_graph.build_sorter(first.edges)
    ranking_one = list(sorter_a.get_ready())
    ranking_two = list(worker_graph.build_sorter(first.edges).get_ready())
    sorter_b = worker_graph.build_sorter(second.edges)
    ranking_three = list(sorter_b.get_ready())   # fresh, same input
    assert ranking_one == ranking_two == ranking_three   # exact ranking

    verdict_one = sched._decide(sched.by_id["B"])
    verdict_two = sched._decide(sched.by_id["B"])
    assert verdict_one == verdict_two == ("dispatch", "proven_safe")

    assert _frontier(sched) == _frontier(sched)  # semantic equality (10)
    assert sched.graph.generation == 1           # no state change happened


# ---------------------------------------------------------------------------
# Invariant 6 -- target eviction & dependency severance
# ---------------------------------------------------------------------------

def test_target_eviction_severs_ready_worker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """helper.py is published while A completes; B is dependency-ready
    against it; the adversarial deletion evicts B straight to BLOCKED
    (existing label) and no dispatch ever touches the orphan."""
    repo = _repo(tmp_path)
    workers = [
        _w("A", declared=["pkg/helper.py"], writes=["pkg/helper.py"],
           cmd=_py([
               "from pathlib import Path",
               ("Path('pkg/helper.py').write_text("
                "'import os\\n\\n\\ndef calc() -> int:\\n"
                "    return len(os.getcwd())\\n')")])),
        _w("B", declared=["pkg/consumer.py"], writes=["pkg/consumer.py"],
           reads=["pkg/helper.py"]),
    ]
    sched = GovernedScheduler(repo, workers, task_id="p23-eviction")

    transitions: list[str] = []
    real_set = sched._set

    def set_hook(worker_id: str, state: str, reason: str | None) -> None:
        if worker_id == "B":
            transitions.append(state)
        return real_set(worker_id, state, reason)

    monkeypatch.setattr(sched, "_set", set_hook)

    real_decide = sched._decide
    memo: dict = {"injected": False, "verdicts": []}

    def decide_hook(worker):
        if (worker["id"] == "B" and not memo["injected"]
                and "pkg/helper.py" in sched.graph.nodes
                and "A" in sched.completed):
            # step 3: reconcile confirms B dependency-ready per the
            # existing state machine (re-decidable + no predecessors)
            assert sched.states["B"]["state"] in ("PENDING", "DEFERRED")
            assert not (sched.worker_graph["edges"].get("B") or set())
            memo["injected"] = True
            # adversarial step: subsequent reconciliation deletes the
            # published target (content=None = removal side of the delta)
            assert sched._reconcile({"pkg/helper.py": None}) is True
            assert "pkg/helper.py" not in sched.graph.nodes
        decision = real_decide(worker)
        if worker["id"] == "B":
            memo["verdicts"].append(decision)
        return decision

    monkeypatch.setattr(sched, "_decide", decide_hook)
    report = sched.run()

    assert memo["injected"] is True, "adversarial deletion never landed"
    verdicts = memo["verdicts"]
    assert verdicts, "B was never evaluated"
    final = verdicts[-1]
    assert final[0] == "block"
    assert final[1].startswith("orphaned_dependency:")
    assert "pkg/helper.py" in final[1]
    assert all(verdict[0] != "dispatch" for verdict in verdicts)
    # strictly to an EXISTING non-ready state label, never RUNNING.
    # (B is re-decided every round while A runs: repeated DEFERRED
    # settlements are contract behavior; the eviction must be THE last
    # transition and RUNNING must never appear.)
    assert transitions[0] == "DEFERRED"
    assert transitions[-1] == "BLOCKED"
    assert transitions.count("BLOCKED") == 1
    assert "RUNNING" not in transitions
    assert sched.state_of("B") == "BLOCKED"
    assert report["status"] == "failed"
    assert report["completed"] == ["A"]
    assert "B" not in report["completed"]


# ---------------------------------------------------------------------------
# Invariant 7 -- dynamic cycles & structured failure evidence
# ---------------------------------------------------------------------------

def test_dynamic_worker_cycle_fails_closed_with_structured_evidence(
        tmp_path: Path) -> None:
    """C's output dynamically derives A<->B worker edges (file graph
    stays acyclic): candidate rejected atomically, both members BLOCKED,
    evidence log machine-readable (generation + members + status)."""
    repo = _repo(tmp_path, {
        "pkg/a_side/__init__.py": "",
        "pkg/b_side/__init__.py": "",
        "pkg/a_side/x.py": "from pkg.b_side import y\n\n"
                           "X_VALUE = y.Y_VALUE\n",
        "pkg/b_side/y.py": "Y_VALUE = 1\n",
        "pkg/b_side/z.py": "from pkg.a_side import w\n\n"
                           "Z_VALUE = w.W_VALUE\n",
        "pkg/a_side/w.py": "W_VALUE = 1\n",
    })
    touched = ("pkg/a_side/__init__.py", "pkg/a_side/x.py",
               "pkg/a_side/w.py", "pkg/b_side/__init__.py",
               "pkg/b_side/y.py", "pkg/b_side/z.py")
    toucher_cmd = _py(
        ["from pathlib import Path"]
        + [f"Path('{rel}').write_text("
           f"Path('{rel}').read_text() + '# touched\\n')" for rel in touched])
    workers = [
        # declared deps hold A/B out of the generation-0 frontier so the
        # DYNAMIC cycle (from C's output) is discovered while both are
        # still schedulable -- then the fail-closed gate drops them.
        _w("A", declared=["pkg/a_side/"], deps=["C"],
           reads=["pkg/b_side/y.py"],
           writes=["pkg/a_side/x.py", "pkg/a_side/w.py"]),
        _w("B", declared=["pkg/b_side/"], deps=["C"],
           reads=["pkg/a_side/w.py"],
           writes=["pkg/b_side/y.py", "pkg/b_side/z.py"]),
        _w("C", declared=["pkg/"], cmd=toucher_cmd),
    ]
    sched = GovernedScheduler(repo, workers, task_id="p23-cycle")
    report = sched.run()

    # deterministic fail-closed: both cycle members dropped to BLOCKED
    assert sched.state_of("C") == "DONE"
    for member in ("A", "B"):
        assert sched.state_of(member) == "BLOCKED"
        assert report["states"][member]["reason"] == "worker_cycle_member"
    assert report["status"] == "failed"
    assert sched.worker_cycle_members == frozenset({"A", "B"})

    # structured, machine-readable failure evidence
    cycle_entries = [entry for entry in sched.reconcile_log
                     if not entry["ok"]
                     and str(entry["reason"]).startswith("worker_cycle:")]
    assert cycle_entries, sched.reconcile_log
    entry = cycle_entries[0]
    members = set(str(entry["reason"]).split(":", 1)[1].split(","))
    assert members == {"A", "B"}
    assert isinstance(entry["generation"], int)
    # candidate was REJECTED: generation stayed put with the old state
    assert entry["generation"] == report["graph"]["generation"] == 0

    blob = json.dumps({"report": report, "log": sched.reconcile_log},
                      sort_keys=True, ensure_ascii=False)
    assert "worker_cycle:A,B" in blob
    parsed = json.loads(blob)
    assert parsed["report"]["states"]["A"]["state"] == "BLOCKED"
    assert parsed["report"]["graph"]["failures"]
    assert any("worker_cycle:A,B" in str(reason)
               for reason in parsed["report"]["graph"]["failures"])
