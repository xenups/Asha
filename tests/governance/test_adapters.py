"""Native + CI adapter tests (Phase F).

Covers: explicit command execution, test failure, pytest rc=5,
lint clean/violations, timeout, interpreter isolation, working-dir
isolation, env propagation, repo cleanliness, CI ingestion
(valid/missing/malformed/mismatched), deterministic parsing, and the
adapter -> GateEvaluator integration seam.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from asha.adapters.ci import CIAdapter
from asha.adapters.native import NativeAdapter
from asha.contracts.execution import ExecutionManifest
from asha.governance.evaluator import (
    VERDICT_FAIL,
    VERDICT_PASS,
    evaluate,
)


def _manifest(
    tmp_path: Path,
    commands: list[list[str]],
    *,
    env_overrides: dict[str, str] | None = None,
    timeout: int = 30,
    working_dir: Path | None = None,
) -> ExecutionManifest:
    return ExecutionManifest(
        run_id="adapter-run-1",
        target_root=tmp_path,
        commands=commands,
        env_overrides=env_overrides or {},
        working_dir=working_dir or tmp_path,
        timeout_seconds=timeout,
    )


PY = sys.executable  # test harness interpreter for FIXTURE commands only
# NOTE: fixture commands use the HARNESS interpreter to create pytest
# subprocesses; the adapter never injects it. Test G proves isolation.


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root)
    (root / "mod.py").write_text("VALUE: int = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return root


class TestNativeExecution:
    def test_explicit_command_success(self, tmp_path: Path) -> None:
        facts = NativeAdapter().execute(
            _manifest(tmp_path, [[PY, "-c", "import sys; sys.exit(0)"]])
        )
        assert facts.exit_codes["cmd0"] == 0

    def test_test_runner_passed(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8")
        cmd = [PY, "-m", "pytest", "-q", "tests"]
        facts = NativeAdapter().execute(_manifest(tmp_path, [cmd]))
        assert facts.test_collection == "COLLECTED"
        assert facts.test_execution == "PASSED"

    def test_test_failure(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_bad.py").write_text(
            "def test_broken():\n    assert False\n", encoding="utf-8")
        cmd = [PY, "-m", "pytest", "-q", "tests"]
        facts = NativeAdapter().execute(_manifest(tmp_path, [cmd]))
        assert facts.test_execution == "FAILED"
        assert facts.failed_test_count >= 1

    def test_pytest_rc5(self, tmp_path: Path) -> None:
        (tmp_path / "empty_test_dir").mkdir()
        # pytest with no tests -> rc 5
        cmd = [PY, "-m", "pytest", "-q", "empty_test_dir"]
        facts = NativeAdapter().execute(_manifest(tmp_path, [cmd]))
        assert facts.test_collection == "NO_TESTS_COLLECTED"
        assert facts.test_execution == "NOT_RUN"

    def test_lint_clean(self, tmp_path: Path) -> None:
        cmd = [PY, "-c", "import sys; sys.exit(0)"]
        facts = NativeAdapter().execute(_manifest(tmp_path, [cmd]))
        # bare command not a linter name -> UNKNOWN, not invented CLEAN
        assert facts.lint_result == "UNKNOWN"

    def test_lint_violations_detected(self, tmp_path: Path) -> None:
        # a command named ruff that exits nonzero
        script = tmp_path / "ruff"
        script.write_text("#!/usr/bin/env python3\nimport sys\n"
                          "print('Found 3 errors')\nsys.exit(1)\n",
                          encoding="utf-8")
        script.chmod(0o755)
        cmd = [str(script)]
        facts = NativeAdapter().execute(_manifest(tmp_path, [cmd]))
        assert facts.lint_result == "VIOLATIONS"
        assert facts.lint_violations_count == 3

    def test_timeout(self, tmp_path: Path) -> None:
        cmd = [PY, "-c", "import time; time.sleep(30)"]
        facts = NativeAdapter().execute(
            _manifest(tmp_path, [cmd], timeout=1)
        )
        assert facts.exit_codes["cmd0"] == -1  # timeout, no unhandled raise

    def test_working_dir_respected(self, tmp_path: Path) -> None:
        wd = tmp_path / "sub"
        wd.mkdir()
        (wd / "marker.txt").write_text("here\n", encoding="utf-8")
        cmd = [PY, "-c",
               "import os; assert os.path.isfile('marker.txt')"]
        facts = NativeAdapter().execute(
            _manifest(tmp_path, [cmd], working_dir=wd)
        )
        assert facts.exit_codes["cmd0"] == 0

    def test_env_overrides_applied_without_mutation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ASHA_ADAPTER_PROBE", raising=False)
        assert "ASHA_ADAPTER_PROBE" not in os.environ
        cmd = [PY, "-c",
               "import os; assert os.environ.get('ASHA_ADAPTER_PROBE')=='yes'"]
        facts = NativeAdapter().execute(
            _manifest(tmp_path, [cmd], env_overrides={"ASHA_ADAPTER_PROBE": "yes"})
        )
        assert facts.exit_codes["cmd0"] == 0
        assert "ASHA_ADAPTER_PROBE" not in os.environ  # parent untouched


class TestInterpreterIsolation:
    def test_asha_interpreter_not_injected(self) -> None:
        # static proof: NativeAdapter source never references sys.executable
        src = (REPO_ROOT / "asha" / "adapters" / "native.py").read_text(
            encoding="utf-8")
        assert "sys.executable" not in src

    def test_explicit_env_interpreter_used(self, tmp_path: Path) -> None:
        """A manifest command carrying its own interpreter is preserved."""
        venv_python = str(PY)  # fixture uses harness interp as the "target"
        cmd = [venv_python, "-c", "import sys; print(sys.executable)"]
        facts = NativeAdapter().execute(_manifest(tmp_path, [cmd]))
        assert facts.exit_codes["cmd0"] == 0


class TestRepoCleanliness:
    def test_repo_untouched_after_execution(self, repo: Path,
                                            tmp_path: Path) -> None:
        cmd = [PY, "-c", "import sys; sys.exit(0)"]
        NativeAdapter().execute(
            _manifest(tmp_path, [cmd], working_dir=repo)
        )
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo,
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout
        assert out == ""


class TestCIAdapter:
    def _report(self, base: Path, *, junit: str | None,
                provenance: dict | None, lint: str | None = None) -> None:
        base.mkdir(parents=True, exist_ok=True)
        if junit is not None:
            (base / "junit.xml").write_text(junit, encoding="utf-8")
        if provenance is not None:
            (base / "provenance.json").write_text(
                json.dumps(provenance), encoding="utf-8")
        if lint is not None:
            (base / "lint.txt").write_text(lint, encoding="utf-8")

    @staticmethod
    def _prov() -> dict:
        return {
            "commit_sha": "c" * 40, "head_sha": "h" * 40,
            "base_sha": "b" * 40, "workflow_id": "wf-1",
        }

    def test_valid_report(self, tmp_path: Path) -> None:
        base = tmp_path / "ci"
        self._report(
            base,
            junit='<?xml version="1.0"?>'
                  '<testsuite tests="3" failures="0" errors="0"/>',
            provenance=self._prov(),
            lint="All checks passed!\n",
        )
        manifest = _manifest(tmp_path, [], env_overrides={
            "ASHA_CI_REPORT": str(base)})
        facts = CIAdapter().execute(manifest)
        assert facts.test_collection == "COLLECTED"
        assert facts.test_execution == "PASSED"
        assert facts.lint_result == "CLEAN"

    def test_missing_report_is_unknown(self, tmp_path: Path) -> None:
        manifest = _manifest(tmp_path, [], env_overrides={
            "ASHA_CI_REPORT": str(tmp_path / "nope")})
        facts = CIAdapter().execute(manifest)
        assert facts.test_execution == "UNKNOWN"
        assert facts.lint_result == "UNKNOWN"

    def test_malformed_junit_is_unknown(self, tmp_path: Path) -> None:
        base = tmp_path / "ci"
        self._report(base, junit="<not-xml", provenance=self._prov())
        manifest = _manifest(tmp_path, [], env_overrides={
            "ASHA_CI_REPORT": str(base)})
        facts = CIAdapter().execute(manifest)
        assert facts.test_execution == "UNKNOWN"

    def test_incomplete_report_is_unknown(self, tmp_path: Path) -> None:
        base = tmp_path / "ci"
        # junit present, provenance MISSING
        self._report(
            base,
            junit='<testsuite tests="3" failures="0" errors="0"/>',
            provenance=None,
        )
        manifest = _manifest(tmp_path, [], env_overrides={
            "ASHA_CI_REPORT": str(base)})
        facts = CIAdapter().execute(manifest)
        assert facts.test_execution == "UNKNOWN"

    def test_provenance_mismatch_is_unknown(self, tmp_path: Path) -> None:
        base = tmp_path / "ci"
        self._report(
            base,
            junit='<testsuite tests="3" failures="0" errors="0"/>',
            provenance={"commit_sha": "x", "head_sha": "y"},  # incomplete
        )
        manifest = _manifest(tmp_path, [], env_overrides={
            "ASHA_CI_REPORT": str(base)})
        facts = CIAdapter().execute(manifest)
        assert facts.test_execution == "UNKNOWN"

    def test_missing_junit_no_optimistic_success(self, tmp_path: Path) -> None:
        base = tmp_path / "ci"
        self._report(base, junit=None, provenance=self._prov())
        manifest = _manifest(tmp_path, [], env_overrides={
            "ASHA_CI_REPORT": str(base)})
        facts = CIAdapter().execute(manifest)
        assert facts.test_execution == "UNKNOWN"
        assert facts.test_execution != "PASSED"
        assert facts.lint_result == "UNKNOWN"

    def test_deterministic_parse(self, tmp_path: Path) -> None:
        base = tmp_path / "ci"
        self._report(
            base,
            junit='<testsuite tests="1" failures="1" errors="0"/>',
            provenance=self._prov(),
        )
        manifest = _manifest(tmp_path, [], env_overrides={
            "ASHA_CI_REPORT": str(base)})
        a = CIAdapter().execute(manifest)
        b = CIAdapter().execute(manifest)
        # SemanticFacts is a Phase-B locked plain class (identity ==);
        # compare every field for deterministic equivalence.
        assert (a.run_id, a.test_collection, a.test_execution,
                a.failed_test_count, a.lint_result,
                a.lint_violations_count, a.raw_logs_ref) == (
            b.run_id, b.test_collection, b.test_execution,
            b.failed_test_count, b.lint_result,
            b.lint_violations_count, b.raw_logs_ref)


class TestAdapterGateEvaluatorIntegration:
    def test_same_facts_same_verdict(self, tmp_path: Path) -> None:
        """Adapter output feeds GateEvaluator unchanged."""
        base = tmp_path / "ci"
        (base).mkdir(parents=True, exist_ok=True)
        (base / "junit.xml").write_text(
            '<testsuite tests="1" failures="1" errors="0"/>',
            encoding="utf-8")
        (base / "provenance.json").write_text(
            json.dumps({"commit_sha": "c" * 40, "head_sha": "h" * 40,
                        "base_sha": "b" * 40, "workflow_id": "w"}),
            encoding="utf-8")
        manifest = _manifest(tmp_path, [], env_overrides={
            "ASHA_CI_REPORT": str(base)})
        facts = CIAdapter().execute(manifest)
        verdict = evaluate(facts)
        assert verdict.verdict == VERDICT_FAIL  # failed tests -> FAIL

    def test_clean_ci_facts_gate_pass(self, tmp_path: Path) -> None:
        base = tmp_path / "ci2"
        base.mkdir(parents=True, exist_ok=True)
        (base / "junit.xml").write_text(
            '<testsuite tests="2" failures="0" errors="0"/>',
            encoding="utf-8")
        (base / "provenance.json").write_text(
            json.dumps({"commit_sha": "c" * 40, "head_sha": "h" * 40,
                        "base_sha": "b" * 40, "workflow_id": "w"}),
            encoding="utf-8")
        (base / "lint.txt").write_text("All checks passed!\n",
                                       encoding="utf-8")
        manifest = _manifest(tmp_path, [], env_overrides={
            "ASHA_CI_REPORT": str(base)})
        facts = CIAdapter().execute(manifest)
        verdict = evaluate(facts)
        assert verdict.verdict == VERDICT_PASS