"""Focused tests for baseline evidence instrumentation (rehearsal-v3 gap).

Scope: persist_baseline_evidence / baseline_checks_recorded only.
Covers: non-mutation + verdict invariance, exceptions, verbatim check
records (exit codes, timeout notes, tails), empty-identities-never-clean,
bounds/redaction, atomic-write failure, and the strip control flow at
the sequential call site.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from asha import check_runner
from asha.common import paths as common_paths
from asha.governance import worker_execution as we
from asha.governance.delta import delta_failures, extract_failures, verdict_for

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _entry(
    nodeid: str = "tests/test_a.py::test_one",
    code: int | None = 1,
    status: str = "failed",
    note: str | None = None,
    tail: str | None = None,
) -> dict:
    return {
        "name": "pytest",
        "scope": "package",
        "status": status,
        "exit_code": code,
        "note": note,
        "output_tail": tail
        or f"FAILED {nodeid} - AssertionError\n1 failed in 1.00s",
    }


def _journal(entries: list[dict]) -> dict:
    return {
        "checks": entries,
        "resolved": {},
        "decision_mode": "COMPLETE",
        "repo": "repo",
        "base": "0" * 40,
        "base_tree": "1" * 40,
        "task_id": "task-1",
    }


def _artifact_path(repo: Path, task_id: str, wid: str) -> Path:
    run_dir = common_paths.get_orchestrator_dir(Path(repo)) / we._safe_id(task_id)
    return run_dir / (we._safe_id(wid) + ".baseline.json")


def _read_artifact(repo: Path, task_id: str, wid: str) -> dict:
    return json.loads(
        _artifact_path(repo, task_id, wid).read_text(encoding="utf-8")
    )


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ASHA_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _seven_failures() -> list[dict]:
    return [
        _entry(f"tests/test_env{f}.py::test_{f}")
        for f in ("_adapters", "_matrix", "sem1", "coverage1", "runpath1", "stepper", "proof")
    ]


# ---------------------------------------------------------------------------
# 1. persistence neither mutates the journal nor changes the verdict
# ---------------------------------------------------------------------------

def test_persistence_never_mutates_journal_or_changes_verdict(tmp_path: Path,
                                                              monkeypatch):
    baseline = _seven_failures()
    fixture = _journal(baseline)
    snapshot = copy.deepcopy(fixture)
    current = _seven_failures()

    v_before = verdict_for(
        extract_failures(current), extract_failures(baseline),
        has_failed_checks=True,
    )
    d_before = delta_failures(
        extract_failures(current), extract_failures(baseline)
    )

    worker = {"id": "w1"}
    monkeypatch.setattr(we, "baseline_checks", lambda *a, **k: fixture)
    journal = we.baseline_checks_recorded(
        worker, tmp_path, "c" * 40, "t" * 40, tmp_path, "task-1"
    )

    # journal input untouched (deep compare), returned value is the input
    assert journal == fixture
    assert fixture == snapshot

    v_after = verdict_for(
        extract_failures(current), extract_failures(journal["checks"]),
        has_failed_checks=True,
    )
    d_after = delta_failures(
        extract_failures(current), extract_failures(journal["checks"])
    )
    assert (v_after, d_after) == (v_before, d_before)

    art = _read_artifact(tmp_path, "task-1", "w1")
    # raw check records preserved byte-equal (JSON round-trip equality)
    assert art["checks"] == baseline
    assert art["execution"] == "ok"
    assert art["error"] is None
    assert art["parse"]["status"] == "completed"
    assert art["parse"]["identity_count"] == 7
    assert art["schema"] == "baseline-evidence/v1"
    # authorization/verdict fields must not exist in the diagnostic payload
    assert "verdict" not in art and "authorized_to_ship" not in art


# ---------------------------------------------------------------------------
# 2. baseline exception: returned None + exception artifact
# ---------------------------------------------------------------------------

def test_baseline_exception_recorded_and_returns_none(tmp_path: Path,
                                                      monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(we, "baseline_checks", boom)
    journal = we.baseline_checks_recorded(
        {"id": "w1"}, tmp_path, "c" * 40, "t" * 40, tmp_path, "task-exc"
    )
    assert journal is None

    art = _read_artifact(tmp_path, "task-exc", "w1")
    assert art["execution"] == "exception"
    assert "resolver exploded" in art["error"]
    assert art["checks"] is None
    assert art["parse"]["status"] == "not_run"


# ---------------------------------------------------------------------------
# 3. failed checks / timeout info / exit codes preserved verbatim
# ---------------------------------------------------------------------------

def test_failed_check_records_preserved_including_timeout_note(tmp_path: Path,
                                                               monkeypatch):
    timeout_entry = {
        "name": "ruff",
        "scope": "package",
        "status": "failed",
        "exit_code": None,
        "note": "timed out after 900s",
        "output_tail": "",
    }
    failed = _entry()
    fixture = _journal([failed, timeout_entry])
    monkeypatch.setattr(we, "baseline_checks", lambda *a, **k: fixture)
    we.baseline_checks_recorded(
        {"id": "w1"}, tmp_path, "c" * 40, "t" * 40, tmp_path, "task-fail"
    )

    art = _read_artifact(tmp_path, "task-fail", "w1")
    assert art["checks"][0] == failed          # exit_code 1, FAILED tail verbatim
    assert art["checks"][1] == timeout_entry   # exit None + timeout note verbatim
    assert art["parse"]["status"] == "completed"
    assert art["parse"]["identity_count"] == 1
    assert art["parse"]["identities"][0]["location"] == "tests/test_a.py::test_one"


# ---------------------------------------------------------------------------
# 4. empty identities are never proof of a clean baseline
# ---------------------------------------------------------------------------

def test_empty_identities_never_imply_clean(tmp_path: Path, monkeypatch):
    cases = []

    # (a) failed check with unparseable output -> zero identities, NOT clean
    unparseable = _entry(tail="traceback...\nno FAILED line here")
    f_a = _journal([unparseable])
    monkeypatch.setattr(we, "baseline_checks", lambda *a, **k: f_a)
    we.baseline_checks_recorded(
        {"id": "wa"}, tmp_path, "c" * 40, "t" * 40, tmp_path, "task-a"
    )
    art_a = _read_artifact(tmp_path, "task-a", "wa")
    cases.append(art_a)
    assert art_a["parse"]["identity_count"] == 0
    assert any(c.get("status") == "failed" for c in art_a["checks"])
    assert art_a["execution"] == "ok"

    # (b) all checks passed -> zero identities (the only clean reading)
    passed = _entry(status="passed", code=0, tail="1 passed in 0.10s")
    f_b = _journal([passed])
    monkeypatch.setattr(we, "baseline_checks", lambda *a, **k: f_b)
    we.baseline_checks_recorded(
        {"id": "wb"}, tmp_path, "c" * 40, "t" * 40, tmp_path, "task-b"
    )
    art_b = _read_artifact(tmp_path, "task-b", "wb")
    cases.append(art_b)
    assert art_b["parse"]["identity_count"] == 0
    assert all(c.get("status") == "passed" for c in art_b["checks"])

    # (c) exception -> no checks at all
    def boom(*a, **k):
        raise ValueError("x")
    monkeypatch.setattr(we, "baseline_checks", boom)
    we.baseline_checks_recorded(
        {"id": "wc"}, tmp_path, "c" * 40, "t" * 40, tmp_path, "task-c"
    )
    art_c = _read_artifact(tmp_path, "task-c", "wc")
    cases.append(art_c)
    assert art_c["checks"] is None
    assert art_c["parse"]["status"] == "not_run"

    # the three zero-identity situations are distinguishable from each other,
    # and no payload carries a clean/success verdict field
    for art in cases:
        assert "clean" not in art and "verdict" not in art
    serialized = [json.dumps(a, sort_keys=True) for a in cases]
    assert len(set(serialized)) == 3


# ---------------------------------------------------------------------------
# 5. bounds + redaction (idempotent defense)
# ---------------------------------------------------------------------------

def test_output_bounds_and_redaction(tmp_path: Path, monkeypatch):
    secret = "SYNTH-TOK-9f3e2xb4"
    monkeypatch.setenv("CI_SYNTHETIC_TOKEN", secret)

    raw_tail = ("y" * 2500) + f" FAILED tests/test_a.py::test_z - boom {secret}"
    raw = _entry(tail=raw_tail)
    f_raw = _journal([raw])
    monkeypatch.setattr(we, "baseline_checks", lambda *a, **k: f_raw)
    we.baseline_checks_recorded(
        {"id": "w1"}, tmp_path, "c" * 40, "t" * 40, tmp_path, "task-red"
    )
    art = _read_artifact(tmp_path, "task-red", "w1")
    saved = art["checks"][0]["output_tail"]
    assert secret not in saved
    assert "[REDACTED]" in saved
    assert len(saved) <= check_runner.TAIL_CHARS

    # pre-processed input passes through byte-identical (no-op)
    pre = _entry(tail="FAILED tests/test_a.py::test_z - boom [REDACTED]")
    f_pre = _journal([pre])
    monkeypatch.setattr(we, "baseline_checks", lambda *a, **k: f_pre)
    we.baseline_checks_recorded(
        {"id": "w2"}, tmp_path, "c" * 40, "t" * 40, tmp_path, "task-pre"
    )
    art2 = _read_artifact(tmp_path, "task-pre", "w2")
    assert art2["checks"][0]["output_tail"] == pre["output_tail"]


# ---------------------------------------------------------------------------
# 6. atomic-write failure: safe, visible, never success-shaped
# ---------------------------------------------------------------------------

def test_artifact_write_failure_is_safe_and_visible(tmp_path: Path,
                                                    monkeypatch, capsys):
    fixture = _journal([_entry()])
    monkeypatch.setattr(we, "baseline_checks", lambda *a, **k: fixture)

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(we, "_atomic_json", broken)
    journal = we.baseline_checks_recorded(
        {"id": "w1"}, tmp_path, "c" * 40, "t" * 40, tmp_path, "task-wf"
    )
    # worker result path unchanged: the journal is still returned intact
    assert journal == fixture

    # no success-shaped artifact fabricated
    assert not _artifact_path(tmp_path, "task-wf", "w1").exists()

    err = capsys.readouterr().err
    assert "Asha: baseline evidence write failed" in err
    assert "OSError" in err and "disk full" in err


# ---------------------------------------------------------------------------
# 7. strip control flow at the sequential call site
# ---------------------------------------------------------------------------

def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_never_passes():\n    assert False\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t",
         "-c", "user.name=t", "commit", "-qm", "base"], check=True,
        capture_output=True)
    return tmp_path


def _worker() -> dict:
    return {
        "id": "w1",
        "cmd": [sys.executable, "-c", "raise SystemExit(0)"],
        "writes": [],
        "reads": [],
    }


def test_baseline_runs_in_its_own_worktree(tmp_path: Path, monkeypatch) -> None:
    """The deferred baseline never shares the candidate worktree: it runs
    on a separate clean checkout of the base revision, which is then
    removed (one baseline pass, one candidate pass)."""
    repo = _make_repo(tmp_path)
    seen: list[Path] = []
    real = we._run_checks

    def _spy(decision, path, resolved, **kw):
        seen.append(Path(path).resolve())
        return real(decision, path, resolved, **kw)

    monkeypatch.setattr(we, "_run_checks", _spy)
    report = we.run_worker_in_worktree(repo, _worker(), task_id="separate-task")
    assert len(seen) == 2, seen            # baseline + candidate, one each
    assert seen[0] != seen[1], seen        # distinct trees
    assert not seen[0].exists(), seen      # baseline worktree cleaned up
    art = _read_artifact(repo, "separate-task", "w1")
    assert art["execution"] == "ok"
    assert report["states"]["w1"]["state"] == "FAILED"  # no-op => uncomparable


def test_baseline_worktree_failure_is_recorded_not_fabricated(
        tmp_path: Path, monkeypatch) -> None:
    """A baseline that cannot run stays unknown: no empty baseline, no
    comparison, and the failure is recorded in the artifact."""
    repo = _make_repo(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("baseline boom")

    monkeypatch.setattr(we, "_baseline_worktree", boom)
    report = we.run_worker_in_worktree(repo, _worker(), task_id="base-task")
    state = report["states"]["w1"]
    assert state["state"] == "FAILED"
    assert state["comparison"]["performed"] is False
    assert state["comparison"]["reason"] == "NOT_COMPARED"
    assert state.get("baseline_failures") is None

    art = _read_artifact(repo, "base-task", "w1")
    assert art["execution"] == "exception"
    assert "baseline boom" in art["error"]
