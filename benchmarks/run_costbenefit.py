"""Phase 2.4 -- cost-benefit benchmark: single vs multi vs Asha.

Compares THREE execution modes on identical tasks, repositories, agent
payloads and success criteria (fairness asserted at runtime):

  M1 single          -- one agent, sequential steps, shared tree, no Asha
  M2 multi           -- thread pool + minimal dep-ordering orchestrator,
                        shared tree, no Asha, no governance (not crippled)
  M3 asha            -- the real GovernedScheduler.run() with all existing
                        governance; integration = copy worker outputs from
                        the (removed-at-end) isolated worktrees.

Agent "work" = deterministic pbkdf2 (no network/LLM available); scales:
S = 200k (~165 ms), L = 800k (~660 ms), calibrated on this host. Content
payloads are byte-identical across modes.

Measured per run (lightweight, harness-side wrappers only -- no
production instrumentation):
  T_total      mode wall (setup & final verification excluded)
  T_agent      sum of agent execution spans  (execute / exec_step)
  T_worker     sum of _run_one spans         (agent + evidence chain)
  T_evidence   sum(_run_one - execute)       (collect/verify/sign work)
  T_wait       T_total - union(create, _run_one, reconcile, decide)
  T_reconcile, T_graph (graph_state.reconcile inside _reconcile),
  T_governance (_decide), T_worktree (dispatcher.create), T_orch (M2),
  T_integration (M3 post-run copy), T_verify (final pytest, all modes)
  tool calls   subprocess invocations by class (git / agent / lint)

Architectural facts this benchmark respects (measured, not assumed):
  * worktrees check out a FIXED base_commit (asha/worktree.py:52,64):
    an isolated worker NEVER sees another worker's output; cross-worker
    data flow is ordering-only at run time. Freshness of consumer
    output is REPORTED per mode, not used as a pass/fail criterion.
  * Prevention events are counted only when they actually occur.

Usage:
  python benchmarks/run_costbenefit.py --task all --reps 9
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from pathlib import Path
from typing import Self

from asha import graph_state
from asha.scheduler import GovernedScheduler

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
REPO_NAME = "repo"
SCALE_S = 200_000          # ~165 ms agent work
SCALE_L = 800_000          # ~660 ms agent work

GITIGNORE = (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
             ".mypy_cache/\n.ruff_cache/\n")
BASE_CORE = {
    ".gitignore": GITIGNORE,
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
}

# ----------------------------------------------------------------- tasks


def agent_cmd(content: str, rel: str, iters: int) -> list[str]:
    """Identical payload in every mode: deterministic agent work, then
    write the exact expected content."""
    code = (
        "import hashlib,pathlib;"
        f"hashlib.pbkdf2_hmac('sha256',b'asha-bench',b'salt',{iters});"
        f"p=pathlib.Path({rel!r});p.parent.mkdir(parents=True,exist_ok=True);"
        f"p.write_text({content!r},encoding='utf-8')"
    )
    return [PY, "-c", code]


CONSUMER_CODE = (
    "import hashlib,pathlib,re;"
    f"hashlib.pbkdf2_hmac('sha256',b'asha-bench',b'salt',{SCALE_S});"
    "t=pathlib.Path('pkg/target.py').read_text(encoding='utf-8');"
    "m=re.search(r'VALUE = (\\d+)',t);"
    "pathlib.Path('pkg/out.py').write_text("
    "f'CONSUMED = {m.group(1)}\\n',encoding='utf-8')"
)


def _task_a(iters: int) -> dict:
    return {
        "scale_iters": iters,
        "baseline": {},
        "workers": [
            {"id": "s_a", "deps": [], "declared": ["pkg/a_out.py"],
             "reads": [], "writes": ["pkg/a_out.py"],
             "cmd": agent_cmd("A_RESULT = 1\n", "pkg/a_out.py", iters)},
            {"id": "s_b", "deps": ["s_a"], "declared": ["pkg/b_out.py"],
             "reads": ["pkg/a_out.py"], "writes": ["pkg/b_out.py"],
             "cmd": agent_cmd("B_RESULT = 2\n", "pkg/b_out.py", iters)},
            {"id": "s_c", "deps": ["s_b"], "declared": ["pkg/c_out.py"],
             "reads": ["pkg/b_out.py"], "writes": ["pkg/c_out.py"],
             "cmd": agent_cmd("C_RESULT = 3\n", "pkg/c_out.py", iters)},
        ],
        "expected": {"pkg/a_out.py": "A_RESULT = 1\n",
                     "pkg/b_out.py": "B_RESULT = 2\n",
                     "pkg/c_out.py": "C_RESULT = 3\n"},
        "freshness": None,
        "adversarial": False,
    }


def _task_b(iters: int) -> dict:
    workers = []
    expected = {}
    for idx in range(1, 5):
        rel = f"pkg/m{idx}.py"
        content = f"M{idx}_VALUE = {idx}\n"
        workers.append({
            "id": f"p_w{idx}", "deps": [], "declared": [rel],
            "reads": [], "writes": [rel],
            "cmd": agent_cmd(content, rel, iters)})
        expected[rel] = content
    return {
        "scale_iters": iters,
        "baseline": {},
        "workers": workers,
        "expected": expected,
        "freshness": None,
        "adversarial": False,
    }


def _task_c(iters: int) -> dict:
    return {
        "scale_iters": iters,
        "baseline": {"pkg/target.py": "VALUE = 1\n"},
        # NO declared dependency between writer and consumer: ordering
        # must come from governance (M3 conflict gate) or NOT at all
        # (M2 naive-concurrent baseline); M1 runs the task's step list
        # sequentially as a single agent naturally would.
        "workers": [
            {"id": "a_writer", "deps": [], "declared": ["pkg/target.py"],
             "reads": [], "writes": ["pkg/target.py"],
             "cmd": agent_cmd("VALUE = 2\n", "pkg/target.py", iters)},
            {"id": "b_consumer", "deps": [],
             "declared": ["pkg/out.py", "pkg/target.py"],
             "reads": ["pkg/target.py"], "writes": ["pkg/out.py"],
             "cmd": [PY, "-c", CONSUMER_CODE]},
            {"id": "c_indep", "deps": [], "declared": ["pkg/indep.py"],
             "reads": [], "writes": ["pkg/indep.py"],
             "cmd": agent_cmd("INDEP = 9\n", "pkg/indep.py", iters)},
        ],
        "expected": {"pkg/target.py": "VALUE = 2\n",
                     "pkg/indep.py": "INDEP = 9\n"},
        "freshness": "pkg/out.py",      # CONSUMED value: reported, not graded
        "adversarial": True,
    }


TASKS = {
    "A": _task_a(SCALE_S),        # sequential: overhead with no payoff
    "B_S": _task_b(SCALE_S),      # parallelizable, small agent work
    "B_L": _task_b(SCALE_L),      # parallelizable, large agent work
    "C": _task_c(SCALE_S),        # adversarial dependency
}

# --------------------------------------------------------------- metrics


class Metrics:
    """Harness-side timing/counting; zero production instrumentation."""

    def __init__(self) -> None:
        self.intervals: list[tuple[str, float, float]] = []
        self.sums: dict[str, float] = {}
        self.counts: dict[str, int] = {}
        self.tool_calls: dict[str, int] = {}
        self.stale_drops = 0
        self.conflict_deferrals = 0

    def span(self, name: str, t0: float, t1: float) -> None:
        self.intervals.append((name, t0, t1))
        self.sums[name] = self.sums.get(name, 0.0) + (t1 - t0)

    def add(self, name: str, value: float = 0.0) -> None:
        self.sums[name] = self.sums.get(name, 0.0) + value

    def bump(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def union_busy(self) -> float:
        """Union length of the work intervals (no double counting)."""
        busy = sorted((t0, t1) for _, t0, t1 in self.intervals)
        total, cur0, cur1 = 0.0, None, None
        for t0, t1 in busy:
            if cur0 is None:
                cur0, cur1 = t0, t1
            elif t0 <= cur1:
                cur1 = max(cur1, t1)
            else:
                total += cur1 - cur0
                cur0, cur1 = t0, t1
        if cur0 is not None:
            total += cur1 - cur0
        return total


class ToolSpy:
    """Count/sum subprocess invocations by class (tool-call metric)."""

    def __init__(self) -> None:
        self.durations: dict[str, float] = {}
        self.counts: dict[str, int] = {}
        self._real_run = subprocess.run
        self._real_popen = subprocess.Popen

    @staticmethod
    def _argv(args, kwargs) -> tuple:
        argv = args[0] if args else kwargs.get("args")
        if argv is None:
            return ()
        if isinstance(argv, (str, bytes)):
            return (argv,)
        return tuple(str(p) for p in argv)

    @classmethod
    def _cls(cls, argv: tuple) -> str:
        if not argv:
            return "other"
        joined = " ".join(argv)
        if "pbkdf2_hmac" in joined or "VALUE = (\\d+)" in joined:
            return "agent"
        if argv[0] == "git":
            return "git"
        if "ruff" in joined or "mypy" in joined or "pytest" in joined:
            return "lint"
        return "other"

    def _record(self, cls: str, dur: float) -> None:
        self.counts[cls] = self.counts.get(cls, 0) + 1
        self.durations[cls] = self.durations.get(cls, 0.0) + dur

    def __enter__(self) -> Self:
        real_run = self._real_run

        def run(*args, **kwargs):
            argv = self._argv(args, kwargs)
            t0 = time.perf_counter()
            try:
                return real_run(*args, **kwargs)
            finally:
                self._record(self._cls(argv), time.perf_counter() - t0)

        def popen(*args, **kwargs):
            argv = self._argv(args, kwargs)
            t0 = time.perf_counter()
            proc = self._real_popen(*args, **kwargs)
            self._record(self._cls(argv), time.perf_counter() - t0)
            return proc

        subprocess.run = run          # type: ignore[assignment]
        subprocess.Popen = popen      # type: ignore[assignment]
        return self

    def __exit__(self, *exc) -> None:
        subprocess.run = self._real_run
        subprocess.Popen = self._real_popen


# -------------------------------------------------------- repo + verify


def make_repo(parent: Path, task: dict) -> Path:
    t0 = time.perf_counter()
    repo = parent / REPO_NAME
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    files = dict(BASE_CORE)
    files.update(task["baseline"])
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo, time.perf_counter() - t0


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)
    return proc.stdout.strip()


def _exec_step(worker: dict, repo: Path, m: Metrics) -> int:
    t0 = time.perf_counter()
    proc = subprocess.run(worker["cmd"], cwd=repo, capture_output=True,
                          text=True, timeout=120)
    t1 = time.perf_counter()
    m.span("run_one", t0, t1)
    m.span("agent", t0, t1)
    m.bump("executions")
    return proc.returncode


def verify(task: dict, repo: Path, mode: str, m: Metrics,
           asha_report: dict | None) -> dict:
    checks, failures = {}, []

    tv0 = time.perf_counter()
    proc = subprocess.run([PY, "-m", "pytest", "tests/", "-q",
                           "--tb=line"], cwd=repo, capture_output=True,
                          text=True, timeout=300)
    m.add("verify", time.perf_counter() - tv0)
    checks["pytest"] = proc.returncode == 0
    if not checks["pytest"]:
        failures.append("pytest failed")

    for rel, want in task["expected"].items():
        path = repo / rel
        got = path.read_text(encoding="utf-8") if path.exists() else None
        ok = got == want
        checks[f"content:{rel}"] = ok
        if not ok:
            failures.append(f"content mismatch {rel}: {got!r}")

    # freshness probe (C): REPORTED per mode, never graded
    freshness = None
    if task["freshness"]:
        out = repo / task["freshness"]
        if out.exists():
            match = re.search(r"CONSUMED = (\d+)",
                              out.read_text(encoding="utf-8"))
            if match:
                freshness = int(match.group(1))
            else:
                failures.append("out.py not parseable (torn write?)")

    # no unauthorized modifications: porcelain == exactly the writes
    # (raw stdout: _git()'s .strip() would eat the status column's
    # leading space of a tracked-modified line)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True,
                            check=True).stdout
    touched = {line[3:].strip() for line in status.splitlines()
               if line.strip()}
    allowed = {rel for w in task["workers"] for rel in w["writes"]}
    checks["no_unauthorized"] = touched == allowed
    if not checks["no_unauthorized"]:
        failures.append(f"touched={sorted(touched)}")

    evidence_ok = None
    if mode == "asha":
        evidence_ok = bool(asha_report) and asha_report["status"] == "ok"
        if not evidence_ok:
            failures.append(
                f"asha status {asha_report and asha_report['status']}")
        else:
            for wid, entry in asha_report["states"].items():
                if entry["state"] != "DONE":
                    failures.append(f"{wid}: {entry['state']}")
                    continue
                ev = entry.get("evidence")
                if ev:
                    ev_path = Path(ev)
                    if not ev_path.is_absolute():
                        ev_path = repo / ev_path
                    if not ev_path.exists():
                        failures.append(f"missing evidence {ev}")
            checks["evidence_valid"] = not any(
                f.startswith(("missing evidence", "INVALID"))
                for f in failures)

    return {"correct": not failures, "failures": failures,
            "checks": checks, "freshness": freshness,
            "verify_s": m.sums.get("verify", 0.0)}


# ------------------------------------------------------------ mode runs


def run_m1(task: dict, parent: Path) -> dict:
    m = Metrics()
    repo, t_setup = make_repo(parent, task)     # setup outside tool counts
    with ToolSpy() as spy:
        t0 = time.perf_counter()
        rcs = []
        for worker in task["workers"]:        # single agent, step list
            rcs.append(_exec_step(worker, repo, m))
        t1 = time.perf_counter()
        m.sums["total"] = t1 - t0
        m.add("orch", 0.0)
        result = verify(task, repo, "single", m, None)
        result.update(_base_result(m, spy, t_setup, rcs, None, None))
        result["mode"] = "single"
        result["repo"] = str(repo)
        return result


def run_m2(task: dict, parent: Path) -> dict:
    m = Metrics()
    deps = {w["id"]: list(w["deps"]) for w in task["workers"]}
    by_id = {w["id"]: w for w in task["workers"]}
    order = [w["id"] for w in task["workers"]]
    repo, t_setup = make_repo(parent, task)     # setup outside tool counts
    with ToolSpy() as spy:
        done: set[str] = set()
        launched: set[str] = set()
        rcs: list[int] = []
        pending: dict = {}
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(order)) as pool:
            while len(done) < len(order):
                ta = time.perf_counter()
                ready = [wid for wid in order
                         if wid not in launched
                         and all(d in done for d in deps[wid])]
                m.add("orch", time.perf_counter() - ta)
                for wid in ready:
                    launched.add(wid)
                    pending[pool.submit(_exec_step, by_id[wid], repo,
                                        m)] = wid
                if not pending:
                    raise RuntimeError("M2 orchestrator deadlock")
                finished, _ = futures_wait(set(pending),
                                           return_when=FIRST_COMPLETED)
                for future in finished:
                    wid = pending.pop(future)
                    rcs.append(future.result())
                    done.add(wid)
        t1 = time.perf_counter()
        m.sums["total"] = t1 - t0
        result = verify(task, repo, "multi", m, None)
        result.update(_base_result(m, spy, t_setup, rcs, None, None))
        result["mode"] = "multi"
        result["repo"] = str(repo)
        return result


def run_m3(task: dict, parent: Path) -> dict:
    m = Metrics()
    repo, t_setup = make_repo(parent, task)
    workers = [{"id": w["id"], "deps": w["deps"],
                "declared_scope": w["declared"], "reads": w["reads"],
                "writes": w["writes"], "cmd": w["cmd"]}
               for w in task["workers"]]
    sched = GovernedScheduler(repo, workers, task_id="bench")
    sched.dispatcher.keep = True          # copy outputs before cleanup

    real_execute, real_run_one = sched.execute, sched._run_one
    real_decide, real_reconcile = sched._decide, sched._reconcile
    real_create = sched.dispatcher.create
    real_graph_reconcile = graph_state.reconcile
    execute_spans: dict[str, tuple] = {}
    injected = {"done": False}

    def execute(worker, path):
        t0 = time.perf_counter()
        try:
            return real_execute(worker, path)
        finally:
            t1 = time.perf_counter()
            execute_spans[str(worker["id"])] = (t0, t1)
            m.span("agent", t0, t1)

    def run_one(worker, path):
        t0 = time.perf_counter()
        try:
            return real_run_one(worker, path)
        finally:
            t1 = time.perf_counter()
            m.span("run_one", t0, t1)
            span = execute_spans.get(str(worker["id"]))
            if span:
                m.add("evidence", (t1 - t0) - (span[1] - span[0]))
            m.bump("executions")

    def decide(worker):
        if (task["adversarial"] and worker["id"] == "b_consumer"
                and not injected["done"] and sched.completed):
            # adversarial step: reconciliation changes the graph
            injected["done"] = True
            sched._reconcile({"pkg/marker.py": "import sys\n\nM = sys.version\n"})
        t0 = time.perf_counter()
        try:
            return real_decide(worker)
        finally:
            m.span("governance", t0, time.perf_counter())

    def reconcile(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return real_reconcile(*args, **kwargs)
        finally:
            m.span("reconcile", t0, time.perf_counter())

    def graph_reconcile(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return real_graph_reconcile(*args, **kwargs)
        finally:
            m.span("graph", t0, time.perf_counter())

    def create(wid):
        t0 = time.perf_counter()
        try:
            return real_create(wid)
        finally:
            m.span("worktree", t0, time.perf_counter())

    sched.execute = execute                       # type: ignore[method-assign]
    sched._run_one = run_one                      # type: ignore[method-assign]
    sched._decide = decide                        # type: ignore[method-assign]
    sched._reconcile = reconcile                  # type: ignore[method-assign]
    sched.dispatcher.create = create              # type: ignore[assignment]
    graph_state.reconcile = graph_reconcile       # type: ignore[assignment]

    with ToolSpy() as spy:
        t0 = time.perf_counter()
        try:
            report = sched.run()
            m.sums["total"] = time.perf_counter() - t0

            # integration: copy worker outputs from isolated worktrees
            ti0 = time.perf_counter()
            for worker in task["workers"]:
                wid = worker["id"]
                if sched.state_of(wid) != "DONE":
                    continue
                wt = sched.dispatcher.paths.get(wid)
                for rel in worker["writes"]:
                    src = wt / rel if wt else None
                    if src and src.exists():
                        shutil.copy2(src, repo / rel)
            m.add("integration", time.perf_counter() - ti0)
            sched.dispatcher.cleanup()

            result = verify(task, repo, "asha", m, report)
        finally:
            # module-level wrappers are GLOBAL: always restore them
            graph_state.reconcile = real_graph_reconcile
        m.stale_drops = len(sched.stale_intents)
        m.conflict_deferrals = len(
            [e for e in sched.deferral_events
             if e["worker"] == "b_consumer"]) if task["adversarial"] else 0
        rcs = [0 if entry["state"] == "DONE" else 1
               for entry in report["states"].values()]
        result.update(_base_result(m, spy, t_setup, rcs, report,
                                   sched))
        result["mode"] = "asha"
        result["repo"] = str(repo)
        return result


def _base_result(m: Metrics, spy: ToolSpy, t_setup: float,
                 rcs: list[int], report, sched) -> dict:
    total = m.sums.get("total", 0.0)
    wait = max(0.0, total - m.union_busy())
    out = {
        "t_setup": t_setup,
        "t_total": total,
        "t_agent": m.sums.get("agent", 0.0),
        "t_worker": m.sums.get("run_one", 0.0),
        "t_evidence": m.sums.get("evidence", 0.0),
        "t_wait": wait,
        "t_reconcile": m.sums.get("reconcile", 0.0),
        "t_graph": m.sums.get("graph", 0.0),
        "t_governance": m.sums.get("governance", 0.0),
        "t_worktree": m.sums.get("worktree", 0.0),
        "t_orch": m.sums.get("orch", 0.0),
        "t_integration": m.sums.get("integration", 0.0),
        "t_verify": m.sums.get("verify", 0.0),
        "executions": m.counts.get("executions", 0),
        "rcs": rcs,
        "tool_calls": dict(spy.counts),
        "tool_time": {k: round(v, 4) for k, v in spy.durations.items()},
        "stale_drops": m.stale_drops,
        "conflict_deferrals": m.conflict_deferrals,
        "asan_status": report["status"] if report else None,
        "rework": 0,
    }
    return out


MODES = {"single": run_m1, "multi": run_m2, "asha": run_m3}

# ------------------------------------------------------------- aggregate


NUMERIC = ["t_setup", "t_total", "t_agent", "t_worker", "t_evidence",
           "t_wait", "t_reconcile", "t_graph", "t_governance",
           "t_worktree", "t_orch", "t_integration", "t_verify",
           "executions"]


def stats(values: list[float]) -> dict:
    return {
        "median": round(statistics.median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "stdev": round(statistics.pstdev(values), 4)
        if len(values) > 1 else 0.0,
    }


def aggregate(task_name: str, mode: str, runs: list[dict]) -> dict:
    cell = {"n": len(runs),
            "correct": sum(1 for r in runs if r["correct"])}
    for key in NUMERIC:
        cell[key] = stats([r[key] for r in runs])
    cell["tool_calls"] = runs[-1]["tool_calls"]
    fresh = [r["freshness"] for r in runs if r["freshness"] is not None]
    if fresh:
        cell["freshness_values"] = sorted(set(fresh))
    cell["stale_drops"] = runs[-1]["stale_drops"]
    cell["conflict_deferrals"] = runs[-1]["conflict_deferrals"]
    return cell


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default="all",
                        choices=["all", *TASKS])
    parser.add_argument("--modes", default="all",
                        choices=["all", *MODES])
    parser.add_argument("--reps", type=int, default=9)
    parser.add_argument("--out", default=str(
        HERE / "results" / "costbenefit.json"))
    args = parser.parse_args()

    task_names = list(TASKS) if args.task == "all" else [args.task]
    mode_names = list(MODES) if args.modes == "all" else [args.modes]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # fairness fingerprint: identical payload + baseline across modes
    for name in task_names:
        task = TASKS[name]
        digest = hashlib.sha256()
        for w in task["workers"]:
            digest.update(" ".join(w["cmd"]).encode())
        digest.update(json.dumps(task["baseline"], sort_keys=True)
                      .encode())
        task["fingerprint"] = digest.hexdigest()

    results: dict = {}
    if out_path.exists():
        results = json.loads(out_path.read_text(encoding="utf-8"))

    for name in task_names:
        task = TASKS[name]
        for mode in mode_names:
            runs = []
            for rep in range(args.reps):
                # unique per invocation/process: Windows can keep .git
                # object files locked briefly after a run; never rmtree
                # a directory that another invocation may still hold.
                stamp = f"{name}-{mode}-{rep}-{time.time_ns()}"
                parent = out_path.parent / "runs" / stamp
                parent.mkdir(parents=True, exist_ok=True)
                runs.append(MODES[mode](task, parent))
                tail = runs[-1]
                print(f"{name:4s} {mode:7s} rep{rep} "
                      f"total={tail['t_total']:.3f}s "
                      f"correct={tail['correct']}", flush=True)
            results.setdefault(name, {})[mode] = aggregate(name, mode,
                                                           runs)
            results[name][mode]["raw"] = [
                {k: v for k, v in r.items() if k != "repo"}
                for r in runs]
            results[name]["fingerprint"] = task["fingerprint"]
            out_path.write_text(json.dumps(results, indent=1),
                                encoding="utf-8")

    # derived tables (§6/§7) over whatever cells exist
    print("\n## median T_total (s)  [single / multi / asha]")
    for name in task_names:
        cells = results.get(name, {})
        row = " / ".join(
            f"{cells[m]['t_total']['median']:.3f}"
            for m in ("single", "multi", "asha") if m in cells)
        print(f"{name:4s} {row}")
    print("\n## governance tax (asha vs multi) and speedup (single/asha)")
    for name in task_names:
        cells = results.get(name, {})
        if {"single", "multi", "asha"} <= set(cells):
            s = cells["single"]["t_total"]["median"]
            mu = cells["multi"]["t_total"]["median"]
            a = cells["asha"]["t_total"]["median"]
            tax = (a - mu) / mu if mu else float("nan")
            print(f"{name:4s} speedup={s / a:.3f} "
                  f"governance_tax={tax:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
