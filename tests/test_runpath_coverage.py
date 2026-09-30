"""Verify check_runner.run() (non-scoped gate path) is also env-aware."""

from __future__ import annotations

import os
from pathlib import Path

from asha import check_runner


def _monorepo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    svc = tmp_path / "services" / "service_a"
    svc.mkdir(parents=True)
    (svc / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    bin_dir = svc / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    recorder = bin_dir / "python"
    recorder.write_text(
        "#!/bin/sh\nprintf 'argv0=%s cwd=%s venv=%s\\n' \"$0\" \"$PWD\" \"$VIRTUAL_ENV\"\n"
        "exit 0\n", encoding="utf-8")
    recorder.chmod(0o755)
    (svc / "tests").mkdir()
    (svc / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8")
    return tmp_path


def test_run_path_uses_service_env(tmp_path: Path) -> None:
    root = _monorepo(tmp_path)
    resolved = {
        "scope": "S2",
        "checks": ["ruff", "pytest"],
        "affected_files": ["services/service_a/tests/test_x.py"],
        "per_file": {"services/service_a/tests/test_x.py": "S2"},
        "base": None,
    }
    results = check_runner.run(root, resolved)
    for entry in results:
        assert entry["status"] in ("passed", "skipped"), entry
    ruff = next(e for e in results if e["name"] == "ruff")
    assert "service_a/.venv/bin/python" in ruff["output_tail"]
    assert "cwd=" in ruff["output_tail"]
    assert "service_a" in ruff["output_tail"]
    # parent env untouched
    assert os.environ.get("VIRTUAL_ENV") is None