"""Phase 2.1 -- behavioral end-to-end: GraphState changes MUST change
scheduler behavior (section 29), over real git worktrees.

Required-list mapping (section 18/19):
  1  test_graph_change_rebuilds_worker_dag
  2  test_derived_dependency_delays_consumer_worker
  3  test_provider_completion_releases_consumer_worker
  4  test_added_dependency_blocks_previously_ready_worker
  8  existing test_multi_worker_completion_reconciles_batch (green)
  9  test_overlapping_completion_regions_reconcile_once
  10  behavioral stale-readiness face inside the delay/release pair
      (existing unit test_stale_generation_intent_rejected stays green)
  11 test_ready_state_invalidated_after_graph_publish
  12 test_deferred_state_reevaluated_against_new_topology
  13 test_running_worker_continues_through_topology_change
  14 test_worker_cycle_fails_closed_without_dispatch
  15 test_failed_projection_preserves_graph_state
  16 test_failed_projection_preserves_worker_graph
  17 test_uncertain_completion_leaves_worker_graph_untouched
  18 test_virtual_ambiguity_fails_closed + test_owner_ambiguity_blocks
  19 test_declared_ordering_survives_reconciliation
  20 the full suite stays green
  19-pair (core regression):
      test_release_pair_and_acquire_pair (both directions)
"""
from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asha
from asha import worker_graph

PY = sys.executable

GITIGNORE = (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
             ".mypy_cache/\n.ruff_cache/\n")
BASELINE = {
    ".gitignore": GITIGNORE,
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    # Target files must pre-exist in EVERY worktree (a concurrent
    # reader's tree never sees a peer's write) and producers CHANGE
    # them, so observed stays non-empty where analysis is required.
    "pkg/f.py": "VALUE = 1\n",
    "pkg/g.py": "VALUE = 1\n",
    "pkg/b.py": "VALUE = 1\n",
    "pkg/dead.py": "DEAD = 1\n",
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


def _w(wid: str, *, declared: Sequence[str] = ("tests/",),
       deps: Sequence[str] = (), reads: Sequence[str] = (),
       writes: Sequence[str] = (), cmd: list[str] | None = None,
       ) -> dict:
    return {"id": wid, "deps": list(deps),
            "declared_scope": list(declared),
            "reads": list(reads), "writes": list(writes),
            "cmd": cmd or [PY, "-c", "pass"]}


def _py(lines: list[str]) -> list[str]:
    return [PY, "-c", "; ".join(lines)]


def _acquire_fixture() -> tuple[list[dict], dict[str, list[str]]]:
    """Round 1: only W0 dispatches (WW/WR conflict serializes the rest).
    W0 plants pkg/f.py -> pkg/g.py facts. Alive owners after W0: f={C},
    g={P} -> derived C dep P. P completes -> g loses its live owner ->
    derived edge disappears -> C releases.

    Returns (workers, cmd_map by id).
    """
    cmds = {
        "W0": _py([
            "from pathlib import Path",
            # pkg/ = package scope: worker outputs must survive the FULL
            # ruff/mypy check chain, so the planted import is USED.
            ("Path('pkg/f.py').write_text("
             "'import pkg.g\\n\\nMARK = pkg.g.VALUE\\n')"),
            "Path('pkg/g.py').write_text('VALUE = 2\\n')",
        ]),
        "P": _py([
            "from pathlib import Path",
            "p = Path('pkg/g.py')",
            "p.write_text(p.read_text() + '# touched\\n')",
        ]),
        "C": _py(["pass"]),
    }
    workers = [
        _w("W0", declared=["pkg/"], writes=["pkg/f.py", "pkg/g.py"],
           cmd=cmds["W0"]),
        _w("P", declared=["pkg/g.py"], reads=["pkg/g.py"],
           writes=["pkg/g.py"], cmd=cmds["P"]),
        _w("C", declared=["pkg/f.py"], reads=["pkg/f.py"],
           writes=["pkg/f.py"], cmd=cmds["C"]),
    ]
    return workers, cmds


def _events_run(tmp_path: Path, task_id: str,
                extra_finish_gate: dict[str, threading.Event] | None = None,
                ) -> tuple[dict, list[tuple[str, str]], asha.GovernedScheduler]:
    repo = _make_repo(tmp_path)
    workers, _ = _acquire_fixture()
    events: list[tuple[str, str]] = []
    finish_gate = extra_finish_gate or {}
    lock = threading.Lock()

    def execute(worker: dict, worktree: Path) -> int:
        wid = str(worker["id"])
        with lock:
            events.append((wid, "start"))
        # optional hold: worker finishes only when its gate is set
        gate = finish_gate.get(wid)
        if gate is not None and not gate.wait(timeout=15):
            return 1
        code = worker["cmd"]
        proc = subprocess.run(code, cwd=worktree, capture_output=True,
                              text=True, timeout=60)
        with lock:
            events.append((wid, "end"))
        return proc.returncode

    sched = asha.GovernedScheduler(repo, workers, task_id=task_id,
                                   execute=execute)
    report = sched.run()
    return report, events, sched


# -- 1: CodeGraph change updates the Worker DAG ------------------------------

def test_graph_change_rebuilds_worker_dag(tmp_path: Path) -> None:
    report, _, sched = _events_run(tmp_path, "p21-rebuild")
    assert report["status"] == "ok", (report["states"], report["reason"])
    # pass 1 (W0) published derived C -> P; pass 2 (P) dropped it.
    derived_passes = [entry.get("derived")
                      for entry in sched.reconcile_log if entry["ok"]]
    assert derived_passes[0] == [("C", "P")], derived_passes
    assert derived_passes[1] == [], derived_passes
    # generation tracks published passes; worker graph rides the same
    # generation (atomic publish, section 17).
    assert report["graph"]["generation"] == report["graph"][
        "reconcile_passes"] == 2
    assert sched.worker_graph["generation"] == sched.graph.generation


# -- 2/4/11: derived dependency delays a previously-ready worker -------------

def test_derived_dependency_delays_consumer_worker(tmp_path: Path) -> None:
    report, events, _ = _events_run(tmp_path, "p21-delay")
    assert report["status"] == "ok", (report["states"], report["reason"])
    starts = [wid for wid, phase in events if phase == "start"]
    # C was dependency-ready at generation 0 but must NOT start before P
    # completed: the rebuilt sorter dropped it from READY (#11).
    assert "W0" in starts and "P" in starts and "C" in starts
    p_end_index = max(i for i, (w, p) in enumerate(events)
                      if (w, p) == ("P", "end"))
    c_start_index = min(i for i, (w, p) in enumerate(events)
                        if (w, p) == ("C", "start"))
    assert c_start_index > p_end_index, events
    assert events[0] == ("W0", "start")  # conflict gate serialized round 1


def test_added_dependency_blocks_previously_ready_worker(
        tmp_path: Path) -> None:
    """#4: C sat in the initial ready set (declared-only topology); the
    first publication adds C's dependency on P, invalidating that
    readiness -- C must not dispatch until P is DONE."""
    report, events, _sched = _events_run(tmp_path, "p21-blocked")
    assert report["status"] == "ok", report
    # before the fix, round 2 dispatched C concurrently with P (both in
    # the stale ready list); now C appears only after P's completion.
    order = [wid for wid, _ in events]
    assert order.index("P") < order.index("C")
    # the deferral that held C in round 1 was the conflict gate, not the
    # dependency -- dependency readiness arrived with the publication.
    assert any(d["worker"] == "C" for d in report["deferral_events"]), \
        report["deferral_events"]


# -- 3: provider completion releases the consumer ---------------------------

def test_provider_completion_releases_consumer_worker(
        tmp_path: Path) -> None:
    report, events, sched = _events_run(tmp_path, "p21-release")
    assert report["status"] == "ok", report
    # derived constraint existed while P was live (pass 1), vanished
    # after P's completion publication (alive-owner death = release).
    assert sched.reconcile_log[0]["derived"] == [("C", "P")]
    assert sched.reconcile_log[1]["derived"] == []
    final = sched.worker_graph["edges"].get("C", set())
    assert "P" not in final
    # hard observable: C started only after P ended, run fully green.
    p_end = max(i for i, (w, p) in enumerate(events)
                if (w, p) == ("P", "end"))
    c_start = min(i for i, (w, p) in enumerate(events)
                  if (w, p) == ("C", "start"))
    assert c_start > p_end, events


# -- 19-pair core regression (both directions, one run each) -----------------

def test_release_pair_and_acquire_pair(tmp_path: Path) -> None:
    """Mandatory pair (section 19):
    ACQUIRE: A and B independent at generation 0; a completion makes the
    consumer depend on the provider; it must NOT dispatch first.
    RELEASE: once the provider completes, the constraint disappears and
    the consumer dispatches."""
    report, events, sched = _events_run(tmp_path, "p21-pair")
    assert report["status"] == "ok", report
    order = [wid for wid, _ in events]
    assert order == ["W0", "W0", "P", "P", "C", "C"], order
    # acquire evidence: first publication carried the derived edge
    first = sched.reconcile_log[0]
    assert first["derived"] == [("C", "P")]
    # release evidence: final worker graph no longer constrains C
    assert "P" not in sched.worker_graph["edges"].get("C", set())


# -- 9: overlapping completion regions coalesce into ONE pass ----------------

def test_overlapping_completion_regions_reconcile_once(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    barrier = threading.Barrier(2)
    workers = [
        _w("A", declared=["pkg/"], writes=["pkg/a.py"],
           cmd=_py(["from pathlib import Path",
                    ("Path('pkg/a.py').write_text("
                     "'import pkg.b\\n\\nMARK = pkg.b.VALUE\\n')")])),
        _w("B", declared=["pkg/"], writes=["pkg/b.py"],
           cmd=_py(["from pathlib import Path",
                    "Path('pkg/b.py').write_text('VALUE = 2\\n')"])),
    ]

    def execute(worker: dict, worktree: Path) -> int:
        proc = subprocess.run(worker["cmd"], cwd=worktree,
                              capture_output=True, text=True, timeout=60)
        try:
            barrier.wait(timeout=15)
        except threading.BrokenBarrierError:
            return 1
        return proc.returncode

    sched = asha.GovernedScheduler(repo, workers, task_id="p21-overlap",
                                   execute=execute)
    report = sched.run()
    assert report["status"] == "ok", report
    # two completions drained in one batch -> exactly one new generation
    # whose file set is the UNION of both observed regions.
    assert len(sched.reconcile_log) == 1
    entry = sched.reconcile_log[0]
    assert set(entry["files"]) == {"pkg/a.py", "pkg/b.py"}
    assert entry["generation"] == 1


# -- 12/13: RUNNING survives; DEFERRED re-decided against new topology -------

def test_running_worker_continues_through_topology_change(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    release = threading.Event()
    workers = [
        _w("SLOW", declared=["tests/slow.py"], writes=["tests/slow.py"],
           cmd=_py(["import time", "time.sleep(0.2)",
                    "from pathlib import Path",
                    "Path('tests/slow.py').write_text('X = 1\\n')"])),
        _w("FAST", declared=["tests/fast.py"], writes=["tests/fast.py"],
           cmd=_py(["from pathlib import Path",
                    "Path('tests/fast.py').write_text('Y = 1\\n')"])),
    ]

    def execute(worker: dict, worktree: Path) -> int:
        wid = str(worker["id"])
        if wid == "SLOW":
            # publish happens while SLOW is demonstrably RUNNING
            release.wait(timeout=10)
            proc = subprocess.run(worker["cmd"], cwd=worktree,
                                  capture_output=True, text=True, timeout=60)
            return proc.returncode
        # FAST finishes first -> triggers reconciliation + rebuild while
        # SLOW is in flight (#13: topology change never terminates it)
        proc = subprocess.run(worker["cmd"], cwd=worktree,
                              capture_output=True, text=True, timeout=60)
        release.set()
        return proc.returncode

    sched = asha.GovernedScheduler(repo, workers, task_id="p21-running",
                                   execute=execute)
    report = sched.run()
    assert report["status"] == "ok", report
    assert sched.state_of("SLOW") == "DONE"
    assert sched.graph.generation >= 1


def test_deferred_state_reevaluated_against_new_topology(
        tmp_path: Path) -> None:
    """#12: DEFERRED is scheduling state, not execution state -- after a
    publication the deferral is re-decided against the fresh topology."""
    report, _, sched = _events_run(tmp_path, "p21-defer")
    assert report["status"] == "ok", report
    deferred_ids = {d["worker"] for d in report["deferral_events"]}
    assert "C" in deferred_ids          # conflict-held in round 1
    assert sched.state_of("C") == "DONE"  # re-decided, dispatched, finished
    # stale deferral reason never outlives the topology: C's final state
    # comes from the rebuilt frontier, not the old ready list.


# -- 14: worker-level cycle fails closed -------------------------------------

def _cycle_repo(tmp_path: Path) -> Path:
    repo = _make_repo(tmp_path)
    _write(repo, "pkg/a_side.py", "import os\n")
    _write(repo, "pkg/b_side.py", "import os\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "sides")
    return repo


def test_worker_cycle_fails_closed_without_dispatch(tmp_path: Path) -> None:
    """declared A dep B + derived B dep A: candidate rejected, old
    GraphState retained, both cycle members blocked, no dispatch."""
    repo = _cycle_repo(tmp_path)
    workers = [
        _w("A", declared=["pkg/a_side.py"], deps=["B"],
           reads=["pkg/a_side.py"],
           cmd=_py(["pass"])),
        _w("B", declared=["pkg/b_side.py"],
           reads=["pkg/b_side.py"],
           cmd=_py(["pass"])),
        _w("W", declared=["pkg/"], writes=["pkg/a_side.py"],
           cmd=_py(["pass"])),
    ]
    sched = asha.GovernedScheduler(repo, workers, task_id="p21-cycle")
    sched.completed = ["W"]  # W finished first: its claims are history
    # Direct seam exercise, section 16's distinction made real:
    # ONE-WAY file edge b -> a = no code-level cycle, but owners differ
    # (B owns b_side, A owns a_side) so the derived edge B dep A
    # collides with declared A dep B = WORKER-level cycle.
    edges = {"pkg/b_side.py": frozenset({"pkg/a_side.py"}),
             "pkg/a_side.py": frozenset()}
    cand = worker_graph.derive(sched.workers, edges,
                               completed=frozenset({"W"}))
    combined = {wid: set(preds) for wid, preds in cand.edges.items()}
    with pytest.raises(worker_graph.WorkerCycleError):
        worker_graph.build_sorter(combined)
    ok = sched._reconcile({"pkg/b_side.py": "import pkg.a_side\n",
                           "pkg/a_side.py": "import os\n"},
                          providers={})
    # both file edges cross owners -> worker cycle -> rejected
    assert ok is False
    assert sched.graph.generation == 0          # #15 old GraphState kept
    assert sched.worker_graph["derived"] == ()  # #16 old WorkerGraph kept
    assert any("worker_cycle" in str(entry.get("reason"))
               for entry in sched.reconcile_log)


# -- 15/16: failure atomicity -------------------------------------------------

def test_failed_projection_preserves_graph_state(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sched = asha.GovernedScheduler(
        repo, [_w("A", declared=["pkg/a.py"], deps=["B"]),
               _w("B", declared=["pkg/b.py"])], task_id="p21-atomic-g")
    before = sched.graph
    ok = sched._reconcile(
        {"pkg/b.py": "import pkg.a\n", "pkg/a.py": "import os\n"},
        providers={})
    assert ok is False
    assert sched.graph is before        # same object reference (§17)


def test_failed_projection_preserves_worker_graph(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sched = asha.GovernedScheduler(
        repo, [_w("A", declared=["pkg/a.py"], deps=["B"]),
               _w("B", declared=["pkg/b.py"])], task_id="p21-atomic-w")
    before = sched.worker_graph
    ok = sched._reconcile(
        {"pkg/b.py": "import pkg.a\n", "pkg/a.py": "import os\n"},
        providers={})
    assert ok is False
    assert sched.worker_graph is before  # never GraphState=new/WG=old


# -- 17: UNKNOWN never becomes EMPTY at the worker-graph face ----------------

def test_uncertain_completion_leaves_worker_graph_untouched(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    workers = [
        _w("U", declared=["tests/u.py"], writes=["tests/u.py"],
           cmd=[PY, "-c", "pass"]),
    ]
    sched = asha.GovernedScheduler(repo, workers, task_id="p21-unknown")
    before = sched.worker_graph
    ok = sched._reconcile(
        {"tests/u.py": "importlib.import_module('m')\n"}, providers={})
    assert ok is False
    assert sched.worker_graph is before
    assert sched.graph.generation == 0
    assert any(str(e.get("reason", "")).startswith("uncertain")
               for e in sched.reconcile_log)


# -- 18: virtual + ownership ambiguity fail closed ---------------------------

def test_virtual_ambiguity_fails_closed(tmp_path: Path) -> None:
    """Two same-batch completions observed the SAME path: the virtual
    analysis view must refuse to guess (never last-write-wins)."""
    repo = _make_repo(tmp_path)
    workers = [
        _w("A", declared=["tests/s.py"], writes=[], reads=["tests/s.py"],
           cmd=_py(["pass"])),
        _w("B", declared=["tests/s.py"], writes=[], reads=["tests/s.py"],
           cmd=_py(["pass"])),
    ]
    sched = asha.GovernedScheduler(repo, workers, task_id="p21-virtual")
    before = sched.graph
    content_a = "import os\n"
    ok = sched._reconcile_batch([
        ("tests/s.py", content_a, "A", "treeA"),
        ("tests/s.py", "import sys\n", "B", "treeB"),
    ])
    assert ok is False
    assert sched.graph is before
    assert any("virtual_ambiguity" in str(e.get("reason"))
               for e in sched.reconcile_log)


def test_owner_ambiguity_blocks_dispatch(tmp_path: Path) -> None:
    """#18/section 6: >=2 live owners of an edge endpoint -> those
    workers are UNKNOWN -> _decide blocks, never guesses."""
    repo = _make_repo(tmp_path)
    workers = [
        # W first: round 1 dispatch order serializes everyone behind it
        # (conflict gate), so the publication sees the expected claims.
        _w("W", declared=["pkg/"], writes=["pkg/f.py", "pkg/g.py"],
           cmd=_py(["from pathlib import Path",
                    ("Path('pkg/f.py').write_text("
                     "'import pkg.g\\n\\nMARK = pkg.g.VALUE\\n')"),
                    "Path('pkg/g.py').write_text('VALUE = 2\\n')"])),
        _w("C1", declared=["pkg/f.py"], reads=["pkg/f.py"],
           cmd=_py(["pass"])),
        _w("C2", declared=["pkg/f.py"], reads=["pkg/f.py"],
           cmd=_py(["pass"])),
        _w("P", declared=["pkg/g.py"], reads=["pkg/g.py"],
           cmd=_py(["pass"])),
    ]
    sched = asha.GovernedScheduler(repo, workers, task_id="p21-owner-amb")
    report = sched.run()
    blocked = {wid: entry for wid, entry in report["states"].items()
               if entry["state"] == "BLOCKED"}
    assert set(blocked) == {"C1", "C2"}, report["states"]
    for entry in blocked.values():
        assert "owner_ambiguous" in str(entry["reason"]), entry
    assert report["graph"]["uncertain_owners"] == ["C1", "C2"]
    assert report["status"] == "failed"   # fail-closed, not silent green


def test_declared_ordering_survives_reconciliation(tmp_path: Path) -> None:
    """#19: declared deps keep their place across every publication."""
    report, events, sched = _events_run(tmp_path, "p21-declared")
    assert report["status"] == "ok", report
    # W0 has no deps and must still finish first in observed order;
    # declared lists survive inside worker_graph every pass.
    assert sched.worker_graph["declared"]["W0"] == []
    assert next(wid for wid, _ in events) == "W0"


# -- 6/7: add / rename invalidation (section 12) ------------------------------


def _guarded_import_cmd(module: str) -> list[str]:
    """Consumer that imports a module which does not exist YET and still
    survives the full ruff+mypy chain in its own worktree. Dotted name
    (pkg.<mod>) so dep_index records the EXACT file node that _resolve
    can promote once pkg/<mod>.py enters the known set; a bare top-level
    name would resolve to <mod>.py at the repo root and never match.
    Dynamic importlib would be UNKNOWN, not a promotable raw target."""
    return _py([
        "from pathlib import Path",
        ("Path('pkg/consumer.py').write_text("
         f"'try:\\n    import {module}  # type: ignore\\n'"
         f" + 'except ImportError:\\n    {module} = None"
         "  # type: ignore\\n\\n'"
         f" + 'MARK = {module}\\n')"),
    ])


def test_newly_created_target_can_become_real_dependency(
        tmp_path: Path) -> None:
    """#6 / section 12 (add): consumer analyzed FIRST while its target
    does not exist -> raw unresolved node; a later worker creates the
    module -> promotion to a real local edge, consumer never reparsed."""
    repo = _make_repo(tmp_path)
    workers = [
        _w("Wc", declared=["pkg/consumer.py"],
           writes=["pkg/consumer.py"],
           cmd=_guarded_import_cmd("pkg.ghostmod")),
        # reads consumer -> conflict-deferred behind Wc: pass 1 lands
        # the consumer's facts BEFORE the module exists.
        _w("W", declared=["pkg/ghostmod.py"], writes=["pkg/ghostmod.py"],
           reads=["pkg/consumer.py"],
           cmd=_py(["from pathlib import Path",
                    "Path('pkg/ghostmod.py').write_text('VALUE = 1\\n')"])),
        _w("C", declared=["pkg/consumer.py"], reads=["pkg/consumer.py"],
           cmd=_py(["pass"])),
    ]
    sched = asha.GovernedScheduler(repo, workers, task_id="p21-add")
    report = sched.run()
    assert report["status"] == "ok", report
    pass_consumer = next(entry for entry in sched.reconcile_log
                         if "pkg/consumer.py" in set(entry["files"]))
    assert pass_consumer["ok"] is True
    # pass 1 could only hold a RAW unresolved target: the module file
    # first shows up in a LATER pass (nothing to resolve against yet),
    # so the final local edge can only be PROMOTION, never direct
    # resolution of the consumer's original facts.
    idx = sched.reconcile_log.index(pass_consumer)
    assert all("pkg/ghostmod.py" not in set(entry["files"])
               for entry in sched.reconcile_log[:idx + 1])
    # the creating worker's pass PROMOTED it to a real local edge
    assert "pkg/ghostmod.py" in set(
        sched.graph.edges.get("pkg/consumer.py") or set())
    promoting = [entry for entry in sched.reconcile_log
                 if "pkg/ghostmod.py" in set(entry["files"])]
    assert promoting, sched.reconcile_log
    assert "pkg/consumer.py" not in set(promoting[-1]["files"]), \
        "consumer must NOT be reparsed for promotion (section 20)"


def test_rename_behaves_as_delete_plus_add(tmp_path: Path) -> None:
    """#7 / section 12 (rename = delete old + add new; NO git rename
    detection): one pass pops the dead node and promotes the consumer's
    raw target to the NEW module name."""
    repo = _make_repo(tmp_path)
    workers = [
        _w("Wc", declared=["pkg/consumer.py"],
           writes=["pkg/consumer.py"],
           cmd=_guarded_import_cmd("pkg.ghost2mod")),
        _w("Wd", declared=["pkg/"], writes=["pkg/ghost2mod.py"],
           reads=["pkg/consumer.py"],
           cmd=_py(["from pathlib import Path",
                    "Path('pkg/dead.py').unlink()",
                    "Path('pkg/ghost2mod.py').write_text('VALUE = 1\\n')"])),
        _w("C", declared=["pkg/consumer.py"], reads=["pkg/consumer.py"],
           cmd=_py(["pass"])),
    ]
    sched = asha.GovernedScheduler(repo, workers, task_id="p21-rename")
    report = sched.run()
    assert report["status"] == "ok", report
    # delete side: the dead node left the graph entirely
    assert "pkg/dead.py" not in sched.graph.nodes
    # add side: consumer now points at the NEW module name
    assert "pkg/ghost2mod.py" in set(
        sched.graph.edges.get("pkg/consumer.py") or set())
