"""K.5.3 regression tests: stale-pyc race in production regression detection.

The DAG `_run_one` ran baseline checks (which create timestamped
__pycache__ .pyc files) and then executed the worker WITHOUT stripping
those caches. When the worker rewrote a source file within the same
wall-clock second, the current-pass pytest trusted the stale .pyc
(pre-change bytecode) -> the new regression silently passed -> DONE.

Proven in K.5.3: PYTHONDONTWRITEBYTECODE=1 makes the defect disappear
(8/8 FAILED); the fix (post-baseline cache strip in _run_one, mirroring
run_worker_in_worktree) makes both preserve modes deterministic.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from asha.governance.dag import run_workers_dag


def _repo_with_app(tmp_path: Path, app_return: int = 1,
                   failing: list[str] | None = None) -> Path:
    """Real repo: app.py (runtime source) + tests asserting app.f()==1."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname = \"k53\"\nversion = \"0.1.0\"\n"
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n"
        "[tool.ruff]\nline-length = 88\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n",
                                     encoding="utf-8")
    (root / "app.py").write_text(f"def f():\n    return {app_return}\n",
                                 encoding="utf-8")
    tests = ["import app\n\n\ndef test_f():\n    assert app.f() == 1\n"]
    for i, _ in enumerate(failing or []):
        tests.append(f"def test_old_{i}():\n    assert False  # pre-existing\n")
    (root / "tests" / "test_app.py").write_text("".join(tests),
                                                encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t",
         "-c", "user.name=t", "commit", "-qm", "base"], check=True,
        capture_output=True)
    return root


def _regression_worker() -> dict:
    """Worker that rewrites app.py f() to return 2 (breaks test_f) and
    commits the change so the DAG captures it in changed_files."""
    mutate = (
        "from pathlib import Path;"
        "import os;"
        "p = Path('app.py');"
        "p.write_text('def f():' + chr(10) + '    return 2' + chr(10));"
        # Force the stale-pyc collision deterministically: the baseline
        # pytest left a timestamped __pycache__/app.pyc; copying ITS mtime
        # onto the rewritten source makes pytest trust the pre-change
        # bytecode (1-second freshness granularity) on the pre-fix code.
        # (single-line `if x: y; z` is a SyntaxError, so use an exec-free
        # expression: os.utime only when the pyc exists, via walrus+and)
        "st = os.stat('__pycache__/app.cpython-312.pyc') "
        "if os.path.exists('__pycache__/app.cpython-312.pyc') else None;"
        "st is not None and os.utime(p, ns=(st.st_mtime_ns, st.st_mtime_ns));"
        "import subprocess;"
        "subprocess.run(['git', 'add', '-A'], check=True);"
        "subprocess.run(['git', '-c', 'user.email=t@t', "
        "'-c', 'user.name=t', 'commit', '-qm', 'worker change'], "
        "check=True)"
    )
    return {"id": "w1", "deps": [], "declared_scope": ["app.py"],
            "reads": ["app.py"], "writes": ["app.py"],
            "cmd": [sys.executable, "-c", mutate]}


@pytest.mark.parametrize("preserve", [False, True])
def test_k53_t2_new_regression_detected_both_preserve_modes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preserve: bool) -> None:
    """T2 equivalent: new regression introduced by a runtime source
    change MUST be detected (BLOCKED/NEW_FAILURES) regardless of the
    preserve_on_failure flag."""
    root = _repo_with_app(tmp_path)
    monkeypatch.setenv("ASHA_STATE_DIR", str(tmp_path / "state"))
    worker = _regression_worker()
    report = run_workers_dag(root, [worker], task_id=f"t-k53-t2-{preserve}",
                             preserve_on_failure=preserve)
    st = report["states"]["w1"]
    assert st["state"] == "FAILED", st
    assert st.get("verdict") == "NEW_FAILURES", st
    deltas = st.get("delta_failures") or []
    assert any("test_f" in str(f.get("location", "")) for f in deltas), st


@pytest.mark.parametrize("preserve", [False, True])
def test_k53_t4_preexisting_plus_new_detected_both_modes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preserve: bool) -> None:
    """T4 equivalent: pre-existing failures + a NEW one. The delta must
    identify the new failure; verdict NEW_REGRESSION in both modes."""
    root = _repo_with_app(tmp_path, failing=["old_1", "old_2", "old_3"])
    monkeypatch.setenv("ASHA_STATE_DIR", str(tmp_path / "state"))
    worker = _regression_worker()
    report = run_workers_dag(root, [worker], task_id=f"t-k53-t4-{preserve}",
                             preserve_on_failure=preserve)
    st = report["states"]["w1"]
    assert st["state"] == "FAILED", st
    assert st.get("verdict") == "NEW_FAILURES", st
    deltas = st.get("delta_failures") or []
    assert any("test_f" in str(f.get("location", "")) for f in deltas), st
    # pre-existing failures preserved in the record, not collapsed
    pre = st.get("baseline_failures") or []
    assert len(pre) == 3, st


def test_k53_t3_preexisting_only_remains_clean_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T3 equivalent: pre-existing failures only, worker introduces
    nothing -> DONE/CLEAN_PASS with an EMPTY delta preserved. The fix
    must not turn pre-existing failures into false regressions."""
    root = _repo_with_app(tmp_path, failing=["old_1", "old_2", "old_3"])
    monkeypatch.setenv("ASHA_STATE_DIR", str(tmp_path / "state"))
    # import keeps python cmd runtime-only for ruff block below
    worker = {"id": "w1", "deps": [],
              "declared_scope": ["tests/test_app.py"],
              "reads": ["tests/test_app.py"], "writes": [],
              "cmd": [sys.executable, "-c", "import subprocess; out=subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True).stdout; subprocess.run(['git', 'add', '-A'], check=True); subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'noop-worker-change'], check=True) if out.strip() else None"]}
    report = run_workers_dag(root, [worker], task_id="t-k53-t3",
                             preserve_on_failure=False)
    st = report["states"]["w1"]
    # pre-existing-only: worker NOT attributed, DONE
    assert st["state"] == "DONE", st
    assert st.get("verdict") == "PRE_EXISTING_ONLY", st
    base_fails = st.get("baseline_failures") or []
    assert len(base_fails) == 3, st
    deltas = st.get("delta_failures")
    assert deltas is not None and deltas == [], st