"""D-2: one execution identity per validation check invocation."""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha import validation

PY = sys.executable
EXEC_ID_RE = re.compile(r"^exec-[0-9a-f]{12}$")
BASE_KEYS = {"name", "scope", "status", "launched", "exit_code",
             "duration_ms", "argv"}


def test_single_check_gets_one_execution_id(tmp_path: Path) -> None:
    entries = validation.run_validation_commands([[PY, "-c", "pass"]], tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["status"] == "passed" and entry["launched"] is True
    assert entry["exit_code"] == 0
    assert EXEC_ID_RE.match(entry["execution_id"])


def test_independent_checks_get_distinct_ids(tmp_path: Path) -> None:
    entries = validation.run_validation_commands(
        [[PY, "-c", "pass"], [PY, "-c", "pass"], [PY, "-c", "pass"]], tmp_path)
    ids = [entry["execution_id"] for entry in entries]
    assert len(set(ids)) == 3
    assert all(EXEC_ID_RE.match(value) for value in ids)
    assert [entry["name"] for entry in entries] == [
        "validation[0]", "validation[1]", "validation[2]"]


def test_failed_check_keeps_its_identity(tmp_path: Path) -> None:
    entries = validation.run_validation_commands(
        [[PY, "-c", "raise SystemExit(7)"]], tmp_path)
    entry = entries[0]
    assert entry["launched"] is True
    assert entry["status"] == "failed" and entry["exit_code"] == 7
    assert EXEC_ID_RE.match(entry["execution_id"])


def test_launch_failure_has_no_execution_identity(tmp_path: Path) -> None:
    entries = validation.run_validation_commands(
        [["/nonexistent/asha-validation-binary"]], tmp_path)
    entry = entries[0]
    assert entry["launched"] is False
    assert entry["status"] == "failed"
    assert "execution_id" not in entry
    assert entry["note"].startswith("launch failed")


def test_timeout_keeps_the_identity_of_the_started_process(tmp_path: Path) -> None:
    entries = validation.run_validation_commands(
        [[PY, "-c", "import time; time.sleep(5)"]], tmp_path, timeout_s=1)
    entry = entries[0]
    assert "timed out" in entry["note"]
    assert EXEC_ID_RE.match(entry["execution_id"])


def test_factory_is_used_once_per_check(tmp_path: Path) -> None:
    minted: list[int] = []

    def factory() -> str:
        minted.append(1)
        return f"exec-{len(minted):012d}"

    entries = validation.run_validation_commands(
        [[PY, "-c", "pass"], [PY, "-c", "raise SystemExit(1)"]], tmp_path,
        execution_id_factory=factory)
    assert len(minted) == 2
    assert [entry["execution_id"] for entry in entries] == [
        "exec-000000000001", "exec-000000000002"]


def test_entry_shape_is_unchanged_apart_from_the_id(tmp_path: Path) -> None:
    entries = validation.run_validation_commands([[PY, "-c", "pass"]], tmp_path)
    keys = set(entries[0]) - {"execution_id"}
    assert BASE_KEYS <= keys
    assert keys - BASE_KEYS == {"output_tail", "stdout_sha256",
                                "stderr_sha256"}
