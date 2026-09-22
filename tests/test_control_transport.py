"""Regression tests for the mandatory --transport gate in .jspace/control.py."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL = REPO_ROOT / ".jspace" / "control.py"
PY = sys.executable


def _run_control(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(CONTROL), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_init_without_transport_fails_closed(tmp_path: Path) -> None:
    proc = _run_control(tmp_path, "init", "--goal", "test", "--next", "verify")
    assert proc.returncode == 1, "omission of --transport must exit 1"
    assert "TRANSPORT GATE" in proc.stderr
    assert not (tmp_path / ".jspace" / "control.json").exists(), (
        "no ledger may be written on a failed gate"
    )


def test_init_with_invalid_transport_fails_closed(tmp_path: Path) -> None:
    proc = _run_control(
        tmp_path, "--transport", "ftp", "init", "--goal", "t", "--next", "n"
    )
    assert proc.returncode == 1
    assert "must be one of" in proc.stderr
    assert not (tmp_path / ".jspace" / "control.json").exists()


def test_init_with_transport_local_succeeds(tmp_path: Path) -> None:
    proc = _run_control(
        tmp_path, "--transport", "local", "init", "--goal", "g", "--next", "n"
    )
    assert proc.returncode == 0, proc.stderr
    ledger = tmp_path / ".jspace" / "control.json"
    assert ledger.exists()
    import json

    state = json.loads(ledger.read_text(encoding="utf-8"))
    assert state["transport"] == "local"
    assert (tmp_path / ".jspace" / "CONTROL.md").exists()


def test_init_with_transport_ssh_succeeds(tmp_path: Path) -> None:
    proc = _run_control(
        tmp_path, "--transport", "ssh", "init", "--goal", "g", "--next", "n"
    )
    assert proc.returncode == 0, proc.stderr
    import json

    state = json.loads((tmp_path / ".jspace" / "control.json").read_text(encoding="utf-8"))
    assert state["transport"] == "ssh"


def test_ledger_transport_is_pinned_per_session(tmp_path: Path) -> None:
    """Once a session declares a transport, mixing transports must be refused."""
    _run_control(tmp_path, "--transport", "local", "init", "--goal", "g", "--next", "n")
    proc = _run_control(tmp_path, "--transport", "ssh", "status")
    assert proc.returncode == 1
    assert "Transport mismatch" in proc.stderr
    good = _run_control(tmp_path, "--transport", "local", "status")
    assert good.returncode == 0
    assert "transport: local" in good.stdout


def test_status_reports_transport(tmp_path: Path) -> None:
    _run_control(tmp_path, "--transport", "local", "init", "--goal", "g", "--next", "n")
    proc = _run_control(tmp_path, "--transport", "local", "status")
    assert proc.returncode == 0
    assert "transport: local" in proc.stdout