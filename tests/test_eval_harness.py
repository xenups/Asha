"""Tests for the empirical evaluation harness (benchmarks/run_eval.py).

Unit-level: dataset schema/integrity, metric math (null never zero-filled),
scope classification (under != over), ORIENT exposure logic, the MEM0
controlled experiment against an injected adapter boundary, report count
formatting, and the dirty-tree refusal. Heavy check-matrix replays are
executed by the harness run itself, not by pytest.
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH = REPO_ROOT / "benchmarks"
RUN_EVAL = BENCH / "run_eval.py"
TASKS = BENCH / "tasks.jsonl"
PY = sys.executable

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import run_eval

from asha import memory, scope_resolver

REQUIRED_FIELDS = {
    "id", "repository", "base_commit", "task_commit", "task_description",
    "keywords", "ground_truth_scope", "ground_truth_scope_source",
    "ground_truth_source", "ground_truth_tests", "ground_truth_outcome",
    "known_failure_modes", "historical_lesson", "control_conflict",
}


class DictBackend:
    available = True
    reason = None

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}

    def add(self, content: str, *, user_id: str, metadata: dict) -> str:
        mid = str(uuid.uuid4())
        self.store[mid] = {"id": mid, "content": content,
                           "metadata": dict(metadata),
                           "created_at": memory._utc_now(),
                           "updated_at": None, "user_id": user_id}
        return mid

    def search(self, query: str, *, user_id: str, top_k: int) -> list[dict]:
        return [dict(r) for r in self.store.values()
                if r["user_id"] == user_id][:top_k]

    def get_all(self, *, user_id: str) -> list[dict]:
        return [dict(r) for r in self.store.values()
                if r["user_id"] == user_id]

    def update(self, memory_id: str, *, text: str | None = None,
               metadata: dict | None = None) -> None:
        record = self.store[memory_id]
        if metadata is not None:
            record["metadata"] = dict(metadata)
        record["updated_at"] = memory._utc_now()

    def delete(self, memory_id: str) -> None:
        del self.store[memory_id]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, timeout=60, check=True)


# ---- 1. dataset schema ----------------------------------------------------

def test_dataset_schema() -> None:
    records, task_sha = run_eval.load_tasks(TASKS)
    assert len(records) >= 5, "sample size must be reported, minimum set"
    assert len(task_sha) == 12
    ids = [r["id"] for r in records]
    assert len(ids) == len(set(ids)), "task ids must be unique"
    for record in records:
        missing = REQUIRED_FIELDS - set(record)
        assert not missing, (record["id"], missing)
        assert record["ground_truth_scope"] in scope_resolver.LEVELS
        assert record["ground_truth_source"], "ground truth must name files"
        assert isinstance(record["ground_truth_scope_source"], str) and \
            record["ground_truth_scope_source"]
        assert isinstance(record["historical_lesson"], (str, type(None)))
        conflict = record["control_conflict"]
        assert set(conflict) == {"fact_key", "fact_value"}
        assert record["keywords"], "baseline search terms must be recorded"


# ---- 2. dataset git integrity --------------------------------------------

def test_dataset_commits_exist_and_bases_match() -> None:
    records, _ = run_eval.load_tasks(TASKS)
    for record in records:
        for sha in (record["task_commit"], record["base_commit"]):
            if sha is None:
                continue
            proc = subprocess.run(
                ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                cwd=REPO_ROOT, capture_output=True, timeout=60)
            assert proc.returncode == 0, (record["id"], sha)
        parent = _git(REPO_ROOT, "log", "-1", "--format=%P",
                      record["task_commit"]).stdout.split()
        expected = parent[0] if parent else None
        assert expected == record["base_commit"], record["id"]
        # ground truth source must actually be part of the commit
        touched = {f for f in _git(REPO_ROOT, "show", "--name-only",
                                   "--format=", record["task_commit"]
                                   ).stdout.split("\n") if f}
        for src in record["ground_truth_source"]:
            assert src in touched, (record["id"], src)


# ---- 3. metric math: unavailable is null, never zero ---------------------

def _entry(scope_class: str, uncertain: bool = False,
           verif_ms: int | None = 1000) -> dict:
    return {
        "task_id": "x", "scope_class": scope_class,
        "scope_uncertain": uncertain,
        "verification": ({'verification_time_ms': verif_ms,
                          'checks': [{'status': 'passed'}]}
                         if verif_ms is not None else None),
    }


def test_metrics_null_not_zero() -> None:
    entries = [_entry("match"), _entry("match"), _entry("under"),
               _entry("over", uncertain=True)]
    agg = run_eval.aggregate(entries, "baseline", 4)
    metrics, counts = agg["metrics"], agg["counts"]
    assert metrics["scope_accuracy"] == 0.5  # 2 / 4
    assert counts["scope_match"] == "2 / 4"
    assert counts["scope_under"] == "1 / 4"
    assert counts["scope_over"] == "1 / 4"
    assert counts["scope_uncertain"] == "1 / 4"
    assert metrics["under_scope_rate"] == 0.25
    assert metrics["over_scope_rate"] == 0.25
    # agent-layer: null with a reason, NEVER 0.0
    for key in ("final_correctness_rate", "source_of_truth_error_rate",
                "mean_time_to_first_correct_hypothesis_ms",
                "regressed_decision_rate"):
        assert metrics[key] is None, key
        assert key in agg["unavailable_reasons"]
    assert metrics["mean_verification_time_ms"] == 1000.0
    # baseline: no orient/mem0 cells -- n/a, not zero
    assert counts["orient_source_exposed"] is None
    assert counts["lesson_retrieved"] is None


# ---- 4. scope class: under != over, uncertain tracked separately ----------

def test_scope_classification_asymmetry() -> None:
    assert run_eval.classify_scope("S1", "certain", "S3") == ("under", False)
    assert run_eval.classify_scope("S3", "certain", "S1") == ("over", False)
    assert run_eval.classify_scope("S2", "certain", "S2") == ("match", False)
    # ambiguity flag rides along but never flips the rank direction
    assert run_eval.classify_scope("S3", "uncertain", "S3") == ("match", True)
    assert run_eval.classify_scope("S3", "uncertain", "S4") == ("under", True)


# ---- 5. dirty tree refuses a canonical run --------------------------------

def test_run_refuses_dirty_tree(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "f@example.com")
    _git(repo, "config", "user.name", "F")
    (repo / "file.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    rc = run_eval.main(["--condition", "baseline", "--repo", str(repo),
                        "--tasks", str(TASKS), "--skip-verification",
                        "--results-dir", str(tmp_path / "out")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "dirty" in err
    assert "--allow-dirty" in err
    assert not (tmp_path / "out").exists(), "no partial canonical results"


# ---- 6. ORIENT exposure logic ---------------------------------------------

def test_orient_exposure(tmp_path: Path) -> None:
    from asha import project_map

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "f@example.com")
    _git(repo, "config", "user.name", "F")
    (repo / ".gitignore").write_text(".jspace/\n", encoding="utf-8")
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "main.py").write_text("def run():\n    return 1\n",
                                 encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_main.py").write_text("def test_ok():\n    assert True\n",
                                        encoding="utf-8")
    (repo / "README.md").write_text("# r\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    orient = project_map.build(repo, "quick", use_cache=False)
    inside = run_eval.orient_exposure(orient, ["pkg/main.py"],
                                      ["tests/test_main.py"])
    assert inside["source_exposed"] is True
    assert inside["test_exposed"] is True
    # root-level file: honestly reported as not exposed, with the reason
    root_level = run_eval.orient_exposure(orient, ["README.md"], [])
    assert root_level["source_exposed"] is False
    assert root_level["test_exposed"] is None
    assert root_level["note"]


# ---- 7. MEM0 controlled experiment: retrieval + precedence -----------------

def test_mem0_probe_retrieval_and_precedence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "f@example.com")
    _git(repo, "config", "user.name", "F")
    (repo / ".gitignore").write_text(".jspace/\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["fastapi"]\n',
        encoding="utf-8")
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "main.py").write_text("X = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    from asha import project_map

    orient = project_map.build(repo, "quick", use_cache=False)
    assert orient["stack"]["language"]["value"] == "python"

    task = {
        "_root": repo,
        "id": "probe",
        "task_description": "fix manifest serialization schema drift",
        "historical_lesson": "manifest serialization previously required "
                              "package-wide regression tests",
        "control_conflict": {"fact_key": "stack.language",
                             "fact_value": "javascript"},
    }
    backend = DictBackend()
    result = run_eval.mem0_probe(
        task, orient,
        "vacation itinerary notes about beaches and flights", backend)

    assert result["available" if "available" in result else
              "lesson_available"] is True
    assert result["lesson_retrieved"] is True, "relevant lesson retrieved"
    assert result["distractor_injected"] is False, (
        "irrelevant memory must not be injected")
    assert result["precedence_current_wins"] is True
    assert result["current_value"] == "python"   # ORIENT wins
    assert result["stored_value"] == "javascript"
    assert result["stale_marked"] is True


# ---- 8b. store isolation: probes never leak across stores (regression) -----

def test_mem0_probe_store_isolation(tmp_path: Path) -> None:
    """Each probe must write ONLY to its own backend: the evaluation run
    isolates one store per task precisely because shared stores confound
    retrieval via top_k truncation over accumulated records."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "f@example.com")
    _git(repo, "config", "user.name", "F")
    (repo / ".gitignore").write_text(".jspace/\n", encoding="utf-8")
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "main.py").write_text("X = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    from asha import project_map

    orient = project_map.build(repo, "quick", use_cache=False)
    task_a = {
        "_root": repo,
        "id": "a",
        "task_description": "alpha task about manifest drift",
        "historical_lesson": "alpha lesson about manifest serialization",
        "control_conflict": {"fact_key": "stack.language",
                             "fact_value": "javascript"},
    }
    task_b = dict(task_a, id="b",
                  task_description="beta task about transport gates",
                  historical_lesson="beta lesson about transport gates")

    backend_a, backend_b = DictBackend(), DictBackend()
    run_eval.mem0_probe(task_a, orient, "unrelated distractor lesson",
                        backend_a)
    run_eval.mem0_probe(task_b, orient, "another unrelated lesson",
                        backend_b)

    # 1 relevant lesson + 1 distractor + 1 control fact == 3, exactly
    assert len(backend_a.store) == 3, "probe A wrote outside its backend"
    assert len(backend_b.store) == 3, "probe B wrote outside its backend"
    a_contents = " ".join(r["content"] for r in backend_a.store.values())
    b_contents = " ".join(r["content"] for r in backend_b.store.values())
    assert "alpha lesson" in a_contents and "beta lesson" not in a_contents
    assert "beta lesson" in b_contents and "alpha lesson" not in b_contents


# ---- 9. report formatting: counts + n/a, no fabricated numbers ------------

def test_report_contains_counts_and_na() -> None:
    agg = run_eval.aggregate([_entry("match"), _entry("match"),
                              _entry("match"), _entry("over")],
                             "baseline", 4)
    run = {
        "metrics": agg["metrics"], "counts": agg["counts"],
        "unavailable_reasons": agg["unavailable_reasons"],
    }
    report = run_eval.render_report("rid", "tasksha", 4, {"baseline": run})
    assert "3 / 4" in report
    assert "1 / 4" in report
    assert "n/a" in report
    assert "never zero-filled" in report or "not zero-filled" in report \
        or "n/a` with a recorded reason" in report
    assert "| Final correctness | n/a |" in report
    assert "0.75" in report  # scope accuracy as exact decimal, not hidden
    # unavailable reasons are enumerated, not silently dropped
    for metric in ("final_correctness_rate", "regressed_decision_rate"):
        assert f"`{metric}`" in report
    # counts, not bare percentages (STEP 19)
    assert "75%" not in report
