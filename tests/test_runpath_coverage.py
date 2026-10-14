"""Verify check_runner.run() (non-scoped gate path) is also env-aware."""

from __future__ import annotations

import os
import shutil
import sys
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
    if sys.platform == "win32":
        # Windows cannot exec a POSIX shell script as an interpreter, and
        # CreateProcess resolves an extensionless `python` to `python.exe`:
        # hardlink the real interpreter under both names, point a venv-style
        # pyvenv.cfg at the base runtime, and let sitecustomize.py act as the
        # argv0/cwd/venv recorder (it exits before `-m` runs). POSIX keeps
        # the shell recorder below.
        def _link(src: Path, dst: Path) -> None:
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
        exe = Path(sys.executable)
        _link(exe, recorder)
        _link(exe, bin_dir / "python.exe")
        (svc / ".venv" / "pyvenv.cfg").write_text(
            f"home = {sys.base_prefix}\n"
            f"version = {sys.version.split()[0]}\n"
            "include-system-site-packages = false\n", encoding="utf-8")
        site_pkgs = svc / ".venv" / "Lib" / "site-packages"
        site_pkgs.mkdir(parents=True, exist_ok=True)
        (site_pkgs / "sitecustomize.py").write_text(
            "import os, sys\n"
            "sys.stdout.write('argv0=%s cwd=%s venv=%s\\n' % (\n"
            "    sys.executable, os.getcwd(),\n"
            "    os.environ.get('VIRTUAL_ENV')))\n"
            "sys.stdout.flush()\n"
            "os._exit(0)\n", encoding="utf-8")
    else:
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
    assert "service_a/.venv/bin/python" in ruff["output_tail"].replace(
        "\\", "/")
    assert "cwd=" in ruff["output_tail"]
    assert "service_a" in ruff["output_tail"]
    # parent env untouched
    assert os.environ.get("VIRTUAL_ENV") is None