"""Tests for the live behavioral benchmark harness (benchmarks/run_live.py).

Covers: condition isolation of prompts (the only intended difference),
ground-truth action classification, trajectory scoring incl. recovery,
null-not-zero aggregation, redaction, trajectory normalization, the live
task-set exclusions, and blinded-verdict merging rules. No codex execution
happens here -- live trajectories are run by the `run` subcommand.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / ".hermes" / "tools"
BENCH = REPO_ROOT / "benchmarks"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import run_live

TASK = {
    "id": "task-x",
    "task_description": "Fix the manifest contract mismatch",
    "base_commit": "b" * 40,
    "ground_truth_source": ["src/writer.py", "src/contract.py"],
    "ground_truth_tests": ["tests/test_contract.py"],
}


@pytest.fixture()
def payloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(run_live, "PAYLOADS", tmp_path)
    (tmp_path / "task-x_orient.json").write_text(
        json.dumps({"marker": "ORIENT_B_AND_C"}), encoding="utf-8")
    (tmp_path / "task-x_memory.json").write_text(
        json.dumps({"marker": "MEMORY_C_ONLY"}), encoding="utf-8")
    return tmp_path


# ---- 1. STEP 3: only the intended difference between conditions ----------

def test_prompt_isolation(payloads: Path) -> None:
    prompt_a = run_live.build_prompt(TASK, "baseline")
    prompt_b = run_live.build_prompt(TASK, "orient")
    prompt_c = run_live.build_prompt(TASK, "orient_mem0")

    assert "Fix the manifest contract mismatch" in prompt_a
    assert "ASHA ORIENT" not in prompt_a
    assert "MEMORY CONTEXT" not in prompt_a

    assert "ORIENT_B_AND_C" in prompt_b and "ORIENT_B_AND_C" in prompt_c
    assert "MEMORY_C_ONLY" not in prompt_b
    assert "MEMORY_C_ONLY" in prompt_c

    # identical shared prefix: task text + requirements byte-for-byte
    shared_a = prompt_a
    assert prompt_b.startswith(shared_a)
    # B and C carry the SAME orient payload section
    orient_b = prompt_b.split("END ORIENT ---")[0]
    orient_c = prompt_c.split("END ORIENT ---")[0]
    assert orient_b == orient_c
    # C alone carries the precedence note
    assert "repository observation wins" in prompt_c
    assert "repository observation wins" not in prompt_b
    # ground truth never leaks into any prompt
    for prompt in (prompt_a, prompt_b, prompt_c):
        assert "ground_truth" not in prompt
        assert "expected_source_of_truth" not in prompt


# ---- 2. STEP 6 classes ---------------------------------------------------

def _event(action: str, files: list[str], index: int = 0) -> dict:
    return {"event_index": index, "action": action,
            "files_touched": files, "t_rel_ms": index * 1000}


def test_classify_action_rules() -> None:
    gt_s, gt_t = run_live.gt_sets(TASK)
    cases = [
        (_event("read", ["src/writer.py"]), "directly_relevant"),
        (_event("read", ["tests/test_contract.py"]), "directly_relevant"),
        (_event("read", ["other/impl.py"]), "wrong_direction"),
        (_event("read", ["pyproject.toml"]), "useful_orientation"),
        (_event("read", ["README.md"]), "useful_orientation"),
        (_event("nav", []), "useful_orientation"),
        (_event("read", ["data/table.csv"]), "irrelevant_exploration"),
        (_event("write", ["src/contract.py"]), "directly_relevant"),
    ]
    for event, expected in cases:
        got = run_live.classify_action(event, gt_s, gt_t)
        assert got == expected, (event, got, expected)


# ---- 3/4. trajectory scoring: correct start and recovery -----------------

def test_score_correct_first_action() -> None:
    events = [
        _event("read", ["src/writer.py"], 0),
        _event("read", ["tests/test_contract.py"], 1),
        _event("write", ["src/contract.py"], 2),
    ]
    scored = run_live.score_trajectory(events, TASK)
    assert scored["first_action_class"] == "directly_relevant"
    assert scored["first_source_correct"] is True
    assert scored["correction_event_index"] is None
    assert scored["recovery_count"] == 0
    assert scored["tool_calls_before_correct_direction"] == 0
    assert scored["wasted_reads"] == []
    assert scored["tool_call_count"] == 3


def test_score_recovery_and_wasted_reads() -> None:
    events = [
        _event("read", ["unrelated/other.py"], 0),   # wrong direction
        _event("read", ["pyproject.toml"], 1),       # orientation
        _event("read", ["notes/todo.txt"], 2),       # wasted
        _event("read", ["src/writer.py"], 3),        # corrected
        _event("write", ["src/writer.py"], 4),
    ]
    scored = run_live.score_trajectory(events, TASK)
    assert scored["first_source_correct"] is False
    assert scored["correction_event_index"] == 3
    assert scored["recovery_count"] == 1
    assert scored["recovery_tool_calls"] == 3
    assert scored["tool_calls_before_correct_direction"] == 3
    assert scored["wasted_reads"] == ["notes/todo.txt",
                                      "unrelated/other.py"]
    assert scored["time_to_first_correct_hypothesis_ms"] == 3000


def test_score_no_substantive_action() -> None:
    scored = run_live.score_trajectory([], TASK)
    assert scored["first_action_class"] is None
    assert scored["first_source_correct"] is None
    assert "note" in scored


# ---- 5/6. aggregation: null never becomes zero ---------------------------

def test_distribution_and_rate() -> None:
    dist = run_live.distribution([3, 1, 2])
    assert dist == {"count": 3, "mean": 2.0, "median": 2, "min": 1,
                    "max": 3}
    assert run_live.distribution([None, None]) is None
    assert run_live.distribution([]) is None

    mixed = run_live.rate([True, False, None])
    assert mixed["k"] == 1 and mixed["N"] == 2
    assert mixed["value"] == 0.5
    empty = run_live.rate([None, None])
    assert empty["value"] is None
    assert "unavailable" in empty  # STEP 18: never zero-filled


# ---- 7. live task set: empty-tree base excluded --------------------------

def test_live_set_exclusion() -> None:
    live = run_live.live_tasks()
    ids = [t["id"] for t in live]
    assert "task-001" not in ids
    assert len(ids) == len(run_live.load_tasks()) - 1
    for task in live:
        assert task["base_commit"] is not None


# ---- 8. redaction ---------------------------------------------------------

def test_redact_secrets() -> None:
    dirty = ("key sk-" + "a" * 30 + " and token=abcdef123456 "
             "Bearer " + "x" * 20 + " normal words pytest S2")
    clean = run_live.redact(dirty)
    assert "sk-" + "a" * 30 not in clean
    assert "abcdef123456" not in clean
    assert "x" * 20 not in clean
    assert "[REDACTED]" in clean
    assert "normal words pytest S2" in clean


# ---- 9. trajectory normalization ------------------------------------------

def test_normalize_events(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    abs_path = str(workspace / "src" / "a.py").replace("\\", "/")
    raw: list[dict] = [
        {"type": "thread.started"},
        {"type": "item.started",
         "item": {"id": "i1", "type": "file_change", "status": "in",
                  "changes": [{"path": abs_path, "kind": "add"}]}},
        {"type": "item.completed",
         "item": {"id": "i2", "type": "command_execution",
                  "command": "cat src/a.py", "exit_code": 0,
                  "status": "completed", "aggregated_output": "x=1"}},
        {"type": "item.completed",
         "item": {"id": "i3", "type": "command_execution",
                  "command": "git log --oneline", "exit_code": 0,
                  "status": "completed", "aggregated_output": "abc"}},
    ]
    for i, event in enumerate(raw):
        event["_t_arrival"] = 1000.0 + i
    events = run_live.normalize_events(raw, workspace,
                                       ["src/a.py"])
    assert len(events) == 4
    assert events[1]["files_touched"] == ["src/a.py"]  # relativized
    assert events[1]["action"] == "write"
    assert events[2]["files_touched"] == ["src/a.py"]  # inventory match
    assert events[2]["action"] == "read"
    assert events[3]["action"] == "nav"
    assert events[3]["files_touched"] == []
    assert events[0]["t_rel_ms"] == 0
    assert events[3]["t_rel_ms"] == 3000
    assert events[2]["tool_arguments"] == "cat src/a.py"


# ---- 10e. report section order (STEP 15) + cohort integrity audit ----------

def test_cohort_integrity_audit(tmp_path: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    live_root = tmp_path / "live-v2"
    monkeypatch.setattr(run_live, "LIVE", live_root)
    task = next(t for t in run_live.live_tasks()
                if t["id"] == "task-002")
    freeze = {"runner": "opencode",
              "model": "opencode/deepseek-v4-flash",
              "asha_commit": "abc123"}

    def make_run(anon: str, condition: str, prompt: str,
                 runner: str = "opencode") -> dict:
        run_dir = live_root / "runs" / anon
        run_dir.mkdir(parents=True)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        return {"run_id": anon, "task_id": "task-002",
                "condition": condition, "runner": runner,
                "model": "opencode/deepseek-v4-flash",
                "asha_commit": "abc123"}

    good = run_live.build_prompt(task, "baseline")
    runs = [make_run("a1", "baseline", good),
            make_run("a2", "orient",
                     run_live.build_prompt(task, "orient")),
            # broken: baseline that got the ORIENT block + a GT path
            make_run("a3", "baseline",
                     good + "\nASHA ORIENT\n"
                     + task["ground_truth_source"][0])]
    integrity = run_live.cohort_integrity(runs, freeze)
    assert integrity["runner_uniform"] is True
    assert integrity["model_uniform"] is True
    assert integrity["asha_commit_uniform"] is True
    assert integrity["prompt_template_mismatches"] == 1
    assert integrity["baseline_gt_path_prompts"] == 1
    assert integrity["condition_label_leaks"] == 1
    assert integrity["valid_per_condition"]["baseline"] == 2
    assert integrity["valid_per_condition"]["orient"] == 1


def test_report_section_order() -> None:
    # STEP 15: integrity in 2; previous cohort before observed;
    # limitations LAST (after interpretation), before the return.
    with open(run_live.__file__, encoding="utf-8") as handle:
        source = handle.read()

    def pos(text: str) -> int:
        found = source.find(text)
        assert found >= 0, f"missing heading: {text}"
        return found

    order = [pos("## 1. Experiment configuration"),
             pos("## 2. Cohort integrity and reconciliation"),
             pos("### Dataset"),
             pos("### Conditions"),
             pos("## 3. Final correctness and A/B/C results"),
             pos("## 4. ORIENT results"),
             pos("## 5. Mem0 results"),
             pos("## 6. Context pollution"),
             pos("## 7. Recovery cost"),
             pos("## 8. Verification cost"),
             pos("## 9. Previous Inconclusive Cohort (not pooled)"),
             pos("## 10. Observed facts"),
             pos("## 11. Interpretation (non-causal)"),
             pos("## 12. Limitations"),
             pos("return '\\n'.join(lines)")]
    assert order == sorted(order), f"section order broken: {order}"


# ---- 10d. runner recovery (live-v2): opencode path + failure taxonomy -----

def test_classify_failure_taxonomy() -> None:
    def make(exit_code=1, timed_out=False, events=(), stderr=''):
        return {'exit_code': exit_code, 'timed_out': timed_out,
                'stderr_tail': stderr,
                'runner': 'opencode',
                'trajectory': {'events': [
                    {'event_type': t} for t in events]}}

    assert run_live.classify_failure(
        make(exit_code=0, events=('text',))) == 'valid'
    assert run_live.classify_failure(
        make(timed_out=True, events=('step_start',))) == 'timeout'
    assert run_live.classify_failure(
        make(events=('error',),
             stderr="You've hit your usage limit")) == 'quota_failure'
    assert run_live.classify_failure(
        make(events=('error',), stderr='401 Unauthorized')
        ) == 'authentication_failure'
    assert run_live.classify_failure(
        make(exit_code=0, events=('step_start',))
        ) == 'invalid_protocol'
    assert run_live.classify_failure(
        make(exit_code=1, events=('step_start', 'tool_use'))
        ) == 'runner_failure'


def test_opencode_completion_marker() -> None:
    finished: dict = {'exit_code': 0, 'timed_out': False,
                      'runner': 'opencode',
                      'trajectory': {'events': [
                          {'event_type': 'step_start'},
                          {'event_type': 'text'}]}}
    assert run_live.is_valid_run(finished) is True
    unfinished: dict = {'exit_code': 0, 'timed_out': False,
                        'runner': 'opencode',
                        'trajectory': {'events': [
                            {'event_type': 'step_start'}]}}
    assert run_live.is_valid_run(unfinished) is False
    # v1 codex records (no runner key) keep the turn.completed rule
    old = {'exit_code': 0, 'timed_out': False,
           'trajectory': {'events': [{'event_type': 'turn.completed'}]}}
    assert run_live.is_valid_run(old) is True


def test_normalize_opencode_events(tmp_path: Path) -> None:
    ws = tmp_path / 'repo'
    ws.mkdir()
    (ws / 'app.py').write_text('x=1\n', encoding='utf-8')
    inv = ['app.py']
    t = 1000.0
    raw = []

    def add(obj):
        nonlocal t
        t += 0.5
        obj['_t_arrival'] = t
        raw.append(obj)

    add({'type': 'tool_use',
         'part': {'tool': 'bash',
                  'state': {'input': {'command': 'ls'},
                            'output': 'app.py',
                            'metadata': {'exit': 0}}}})
    add({'type': 'tool_use',
         'part': {'tool': 'write',
                  'state': {'input': {'filePath': str(ws / 'new.py')},
                            'output': 'ok'}}})
    add({'type': 'text', 'part': {'text': 'done'}})
    events = run_live.normalize_opencode(raw, ws, inv)
    assert [e['action'] for e in events] == ['nav', 'write', None]
    assert events[1]['files_touched'] == ['new.py']
    assert events[2]['tool_result_summary'] == 'done'
    assert events[0]['t_rel_ms'] == 0 and events[2]['t_rel_ms'] == 1000


def test_experiment_cohorts_never_share_paths(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import os
    monkeypatch.setenv('ASHA_BENCH_EXPERIMENT', 'live-v2')
    monkeypatch.setenv('ASHA_BENCH_RUNNER', 'opencode')
    reloaded = importlib.reload(run_live)
    try:
        assert reloaded.LIVE.name == 'live-v2'
        assert reloaded.RUNNER == 'opencode'
        assert reloaded.MODEL == 'opencode/deepseek-v4-flash'
        assert str(reloaded.FREEZE).endswith(
            'live-v2' + os.sep + '_freeze.json')
    finally:
        monkeypatch.delenv('ASHA_BENCH_EXPERIMENT')
        monkeypatch.delenv('ASHA_BENCH_RUNNER')
        importlib.reload(run_live)
    assert run_live.LIVE.name == 'live'
    assert run_live.MODEL == 'gpt-5.6-terra'


# ---- 10b. validity marking: partial/failed runs never count ---------------

def test_is_valid_run_rules() -> None:
    def make(**over):
        base = {"exit_code": 0, "timed_out": False,
                "trajectory": {"events": [
                    {"event_type": "thread.started"},
                    {"event_type": "turn.completed"}]}}
        base.update(over)
        return base

    assert run_live.is_valid_run(make()) is True
    assert run_live.is_valid_run(make(exit_code=1)) is False
    assert run_live.is_valid_run(make(timed_out=True)) is False
    broken = make()
    broken["trajectory"]["events"] = [
        {"event_type": "thread.started"},
        {"event_type": "error"},
        {"event_type": "turn.failed"}]
    assert run_live.is_valid_run(broken) is False


# ---- 10c. blinding fix: correctness package is condition-neutral ---------

def test_blind_package_condition_neutral(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live_root = tmp_path / "live"
    run_dir = live_root / "runs" / "anon9"
    run_dir.mkdir(parents=True)
    payloads = tmp_path / "payloads"
    payloads.mkdir()
    (payloads / "task-002_memory.json").write_text(
        json.dumps({"retrieved": [{"id": "m1"}]}), encoding="utf-8")
    monkeypatch.setattr(run_live, "LIVE", live_root)
    monkeypatch.setattr(run_live, "PAYLOADS", payloads)

    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "anon9", "task_id": "task-002",
        "condition": "orient_mem0", "replicate": 1,
        "metrics": {"verification": {"failed_checks": []}},
    }), encoding="utf-8")
    # an invalid run must produce NO package at all
    bad_dir = live_root / "runs" / "anon8"
    bad_dir.mkdir(parents=True)
    (bad_dir / "run.json").write_text(json.dumps({
        "run_id": "anon8", "task_id": "task-002",
        "condition": "baseline", "replicate": 2,
        "invalid": {"reason": "runner_error_event"},
        "metrics": {},
    }), encoding="utf-8")

    rc = run_live.cmd_blind(None)
    assert rc == 0
    judge_root = live_root / "_judge"
    packages = sorted(p.name for p in judge_root.glob("*.json"))
    assert packages == ["anon9.json"], (
        "filename must be the opaque run id only, and invalid runs "
        "must not be judged")
    package = json.loads(
        (judge_root / "anon9.json").read_text(encoding="utf-8"))
    assert "condition" not in package
    assert "task_id" not in package
    assert "memory_context_shown_to_agent" not in package
    assert "memory_impact" not in str(package.get(
        "questions_for_evaluator"))
    # memory context lives only in the separate sidecar
    sidecar = judge_root / "_memory_context" / "anon9.json"
    assert sidecar.exists()
    side = json.loads(sidecar.read_text(encoding="utf-8"))
    assert side["anon_id"] == "anon9"
    assert side["memory_context"]["retrieved"][0]["id"] == "m1"


# ---- 10. blinded verdict merging ------------------------------------------

def test_apply_judgements_rules(tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture) -> None:
    live_root = tmp_path / "live"
    runs_dir = live_root / "runs" / "anon1"
    runs_dir.mkdir(parents=True)
    monkeypatch.setattr(run_live, "LIVE", live_root)
    mapping_path = tmp_path / "mapping.json"
    monkeypatch.setattr(run_live, "MAPPING", mapping_path)
    mapping_path.write_text(json.dumps(
        {"anon1": {"task_id": "task-x", "condition": "orient",
                   "replicate": 1}}), encoding="utf-8")
    (runs_dir / "run.json").write_text(json.dumps({
        "run_id": "anon1",
        "metrics": {"verification": {"failed_checks": []}},
        "evaluation": {},
    }), encoding="utf-8")

    def verdict(final: str, source: str, reason: str = "r") -> dict:
        return {"final": final, "source_of_truth_reasoning": source,
                "reason": reason}

    def apply(v: dict) -> dict:
        verdicts = tmp_path / "verdicts.json"
        verdicts.write_text(json.dumps({"anon1": v}), encoding="utf-8")
        rc = run_live.apply_judgements(
                type("A", (), {"verdicts": str(verdicts)})())
        assert rc == 0
        return json.loads((runs_dir / "run.json").read_text(
            encoding="utf-8"))

    ok = apply(verdict("correct", "yes - built on src/writer.py"))
    assert ok["metrics"]["final_correct"] is True

    partial = apply(verdict("partial", "yes"))
    assert partial["metrics"]["final_correct"] is False

    wrong_source = apply(verdict("correct", "no - ignored contract"))
    assert wrong_source["metrics"]["final_correct"] is False

    failing = verdict("correct", "yes")
    (runs_dir / "run.json").write_text(json.dumps({
        "run_id": "anon1",
        "metrics": {"verification": {"failed_checks": ["pytest"]}},
        "evaluation": {},
    }), encoding="utf-8")
    applied = apply(failing)
    assert applied["metrics"]["final_correct"] is False
    assert applied["evaluation"]["final"] == "correct"
