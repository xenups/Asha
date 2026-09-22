"""Regression tests: questions ledger invariant (dict, never list).

The historical bug: `questions` was created/validated as a list while
`gate()` and `shared_context()` consumed it with dict methods (`.items()`,
`.values()`). Both shapes are now pinned: dict keyed by str qid, and a
legacy list must be rejected by validate().
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL = REPO_ROOT / ".jspace" / "control.py"
PY = sys.executable


def _load_control():
    spec = importlib.util.spec_from_file_location("control_under_test", CONTROL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _control(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(CONTROL), "--transport", "local", "--root", str(cwd), *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )


def test_default_questions_is_dict() -> None:
    control = _load_control()
    state = control.default_state(Path("."), "medium")
    assert state["questions"] == {}
    assert isinstance(state["questions"], dict)


def test_validate_rejects_legacy_list() -> None:
    control = _load_control()
    state = control.default_state(Path("."), "medium")
    state["goal"] = "g"
    state["next"] = "n"
    state["transport"] = "local"
    state["questions"] = [{"question": "q?", "checkpoint": 1, "closed": False}]
    with pytest.raises(control.ControlError, match="must be a dict"):
        control.validate(state)


def test_validate_accepts_dict_and_shared_context_runs() -> None:
    control = _load_control()
    state = control.default_state(Path("."), "medium")
    state["goal"] = "g"
    state["next"] = "n"
    state["transport"] = "local"
    state["questions"]["1"] = {
        "question": "open issue?", "checkpoint": 1, "closed": False,
    }
    control.validate(state)  # must not raise
    context = control.shared_context(state)
    assert "open issue?" in context


def test_gate_ship_refuses_with_open_dict_question() -> None:
    control = _load_control()
    import time

    state = control.default_state(Path("."), "medium")
    state["goal"] = "g"
    state["next"] = "n"
    state["transport"] = "local"
    # Valid read receipts (check_reads runs before the ship-specific checks).
    agent = state["agents"]["root"]
    for name in ("SKILL.md", "modules/self-monitoring.md"):
        data = (REPO_ROOT / name).read_bytes()
        agent["reads"][name] = {"sha256": control.digest(data),
                                "time": time.time()}
    agent["broadcast"] = False
    state["questions"]["1"] = {
        "question": "still open?", "checkpoint": 1, "closed": False,
    }
    with pytest.raises(control.ControlError, match="Open questions"):
        control.gate(Path("."), state, "root", "ship")


def test_cli_question_lifecycle_roundtrip(tmp_path: Path) -> None:
    """init -> read -> open -> close -> status -> check work, end to end."""
    init = _control(tmp_path, "init", "--goal", "g", "--next", "n")
    assert init.returncode == 0, init.stderr

    read = _control(tmp_path, "read", "SKILL.md", "modules/self-monitoring.md")
    assert read.returncode == 0, read.stderr

    # Closed questions must reference an existing checkpoint with evidence.
    proof = tmp_path / "proof.txt"
    proof.write_text("checkpoint proof\n", encoding="utf-8")
    checkpoint = _control(tmp_path, "checkpoint", "--claim", "baseline",
                          "--evidence", "proof.txt")
    assert checkpoint.returncode == 0, checkpoint.stderr

    opened = _control(tmp_path, "question", "--open", "ship blocker?")
    assert opened.returncode == 0, opened.stderr
    assert "question 1 open" in opened.stdout

    ledger = tmp_path / ".jspace" / "control.json"
    state = json.loads(ledger.read_text(encoding="utf-8"))
    assert isinstance(state["questions"], dict), "serialized shape must be dict"
    assert state["questions"]["1"]["question"] == "ship blocker?"

    evidence_file = tmp_path / "notes.txt"
    evidence_file.write_text("evidence of closure\n", encoding="utf-8")
    closed = _control(tmp_path, "question", "--close", "1",
                      "--evidence", "notes.txt")
    assert closed.returncode == 0, closed.stderr

    status = _control(tmp_path, "status", "--json")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["questions"]["1"]["closed"] is True

    # Closed question must chain to the checkpoint record; proof.txt stays
    # byte-identical or current_evidence() would correctly refuse.
    gate = _control(tmp_path, "check", "--stage", "work")
    assert gate.returncode == 0, gate.stderr
    assert "GATE WORK: PASS" in gate.stdout
