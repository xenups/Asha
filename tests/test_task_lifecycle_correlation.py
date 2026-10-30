"""Phase 1 -- task lifecycle correlation.

Task identity is injectable per call (no global state), the execution id
is recorded inside the sealed evidence, and the pipeline journal carries
the ids in its payload without changing the five-field event contract.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha import mcp_server as server  # noqa: E402
from asha import telemetry, validation  # noqa: E402
from asha.common import paths as common_paths  # noqa: E402

PY = sys.executable
TASK_ID_RE = re.compile(r"^mcp-[0-9a-f]{12}$")
EXEC_ID_RE = re.compile(r"^exec-[0-9a-f]{12}$")

BASELINE = {"pkg/base.py": "def base_helper(y: int) -> int:\n    return y\n",
            "pkg/user.py": ("from pkg.base import base_helper\n"
                            "\n"
                            "def use_base(y: int) -> int:\n"
                            "    return base_helper(y)\n")}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)
    return proc.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo / ".gitignore", ".jspace/\n__pycache__/\n")
    for rel, text in BASELINE.items():
        _write(repo / rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _journal_events(repo: Path, run_id: str) -> list[dict]:
    path = common_paths.get_journal_dir(repo) / (run_id + ".jsonl")
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _dispatch(repo: Path, task_id: str, *, cmd: list[str],
              declared: list[str], wid: str = "w1") -> dict:
    return server.asha_dispatch_task(
        {"id": wid, "declared_scope": declared, "cmd": cmd,
         "root": str(repo), "execution_mode": "in_place_atomic"},
        task_id_factory=lambda: task_id)


# ------------------------------------------------------- task identity ---

def test_task_id_default_format_and_uniqueness() -> None:
    ids = {server._new_task_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(TASK_ID_RE.match(value) for value in ids)


def test_task_id_factory_called_once_per_task() -> None:
    calls: list[int] = []

    def factory() -> str:
        calls.append(1)
        return "mcp-aaaaaaaaaaaa"

    assert server._new_task_id(factory) == "mcp-aaaaaaaaaaaa"
    assert len(calls) == 1


def test_dispatch_injected_task_id_is_the_identity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    payload = _payload(_dispatch(
        repo, "mcp-111111111111",
        cmd=[PY, "-c", "open('pkg/base.py', 'a').write('NEW = 1\\n')"],
        declared=["pkg/base.py"]))
    assert payload["state"] == "DONE"
    assert payload["task_id"] == "mcp-111111111111"
    assert EXEC_ID_RE.match(payload["execution_id"])
    assert payload["execution"]["execution_id"] == payload["execution_id"]


def test_two_tasks_keep_independent_identities(tmp_path: Path) -> None:
    # one repo per task: an in-place run on a dirty tree treats the earlier
    # run's uncommitted change as out-of-scope, which is the scope contract,
    # not a correlation signal.
    repo = _repo(tmp_path / "a")
    other = _repo(tmp_path / "b")
    first = _payload(_dispatch(
        repo, "mcp-222222222222",
        cmd=[PY, "-c", "open('pkg/base.py', 'a').write('A = 1\\n')"],
        declared=["pkg/base.py"], wid="wa"))
    second = _payload(_dispatch(
        other, "mcp-333333333333",
        cmd=[PY, "-c", "open('pkg/user.py', 'a').write('B = 2\\n')"],
        declared=["pkg/user.py"], wid="wb"))
    assert first["task_id"] != second["task_id"]
    assert first["execution_id"] != second["execution_id"]
    for payload, task_id, worker, where in (
            (first, "mcp-222222222222", "wa", repo),
            (second, "mcp-333333333333", "wb", other)):
        assert payload["state"] == "DONE", payload
        events = _journal_events(where, task_id)
        assert [event["event_type"] for event in events] == [
            "execution_started", "sealed"]
        for event in events:
            assert event["run_id"] == task_id
            assert event["payload"]["task_id"] == task_id
            assert event["payload"]["worker_id"] == worker
            assert event["payload"]["execution_id"] == payload["execution_id"]


# ---------------------------------------------------- execution identity ---

def test_execution_record_gets_unique_ids() -> None:
    def record(**over: object) -> dict:
        kwargs = {"started_at": "2026-01-01T00:00:00+00:00", "duration_ms": 1,
                  "exit_code": 0, "termination": "exited", "stdout": "",
                  "stderr": "", "known_policy_command": False}
        kwargs.update(over)
        return validation.execution_record(["true"], ".", **kwargs)  # type: ignore[arg-type]

    first = record()
    second = record()
    assert EXEC_ID_RE.match(str(first["execution_id"]))
    assert first["execution_id"] != second["execution_id"]
    assert record(execution_id="exec-fixed")["execution_id"] == "exec-fixed"


# ------------------------------------------------- payload correlation ---

def test_with_correlation_adds_only_existing_ids() -> None:
    merged = telemetry.with_correlation({"worker_id": "w1"},
                                        task_id="t1", execution_id=None)
    assert merged == {"worker_id": "w1", "task_id": "t1"}


def test_with_correlation_rejects_collision_and_unknown_field() -> None:
    with pytest.raises(ValueError):
        telemetry.with_correlation({"task_id": "t1"}, task_id="t2")
    with pytest.raises(ValueError):
        telemetry.with_correlation({}, run_id="r1")


def test_serialization_contract_is_unchanged() -> None:
    event = {"event_id": "e1", "run_id": "r1",
             "timestamp": "2026-01-01T00:00:00+00:00",
             "event_type": "sealed",
             "payload": telemetry.with_correlation({"evidence_id": None},
                                                   task_id="t1")}
    decoded = json.loads(telemetry.serialize_event(event))
    assert sorted(decoded) == ["event_id", "event_type", "payload", "run_id",
                               "timestamp"]
    assert decoded["payload"]["task_id"] == "t1"


def test_legacy_event_without_correlation_still_reads(tmp_path: Path) -> None:
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps({"event_id": "e0", "run_id": "r0",
                                "timestamp": "2026-01-01T00:00:00+00:00",
                                "event_type": "change_detected",
                                "payload": {"target_files": ["a.py"]}},
                               sort_keys=True) + "\n", encoding="utf-8")
    events, integrity = telemetry.read_events(path.read_text(encoding="utf-8"))
    assert len(events) == 1
    assert integrity.incomplete is False
    assert "task_id" not in events[0]["payload"]


# ------------------------------------------------------- failure path ---

def test_failed_execution_journals_execution_failed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    payload = _payload(_dispatch(
        repo, "mcp-444444444444",
        cmd=[PY, "-c",
             "open('pkg/base.py', 'a').write('BAD = 1\\n'); raise SystemExit(1)"],
        declared=["pkg/base.py"]))
    assert payload["state"] == "FAILED"
    events = _journal_events(repo, "mcp-444444444444")
    assert [event["event_type"] for event in events] == [
        "execution_started", "execution_failed"]
    terminal = events[-1]["payload"]
    assert terminal["task_id"] == "mcp-444444444444"
    assert terminal["execution_id"] == payload["execution_id"]
    assert terminal["evidence"] is None


# ------------------------------------------------- sealed evidence binding ---

def test_sealed_evidence_carries_correlation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    payload = _payload(_dispatch(
        repo, "mcp-555555555555",
        cmd=[PY, "-c", "open('pkg/base.py', 'a').write('OK = 1\\n')"],
        declared=["pkg/base.py"], wid="wz"))
    evidence_path = Path(payload["evidence"]["path"])
    sealed = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert sealed["task_id"] == "mcp-555555555555"
    assert sealed["worker_id"] == "wz"
    assert sealed["execution_id"] == payload["execution_id"]


# ------------------------------------------------------- run_spec seam ---

def test_run_spec_accepts_task_id_factory(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = {"workers": [{"id": "w1", "deps": [],
                         "declared_scope": ["pkg/base.py"],
                         "cmd": [PY, "-c",
                                 "open('pkg/base.py', 'a').write('S = 1\\n')"]}]}
    result = server.asha_run_spec(
        {"root": str(repo), "spec_content": spec, "apply": True},
        task_id_factory=lambda: "mcp-666666666666")
    assert not result.get("isError"), result
    assert _payload(result)["task_id"] == "mcp-666666666666"
