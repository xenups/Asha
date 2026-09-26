"""NativeAdapter — executes an ExecutionManifest in its designated
environment (Phase F).

Contract:
* preserves the manifest command VERBATIM — never injects Asha's own
  interpreter or rewrites an opaque command;
* applies env_overrides without mutating the parent environment;
* enforces working_dir and timeout;
* captures stdout/stderr into external scratch (Phase D Zone 3);
* normalizes results into SemanticFacts ONLY — no policy vocabulary.

Command -> fact classification (deterministic adapter rule, not an
environment guess): a command whose argv tokens name a test runner
(pytest) maps to test facts; linters (ruff/flake8) map to lint facts.
Anything unclassifiable maps to UNKNOWN states (never fabricated).

Test normalization (Phase C semantics):
  rc 0            -> PASSED / COLLECTED
  rc 5 (pytest)   -> NO_TESTS_COLLECTED / NOT_RUN
  other nonzero   -> FAILED
  timeout/spawn   -> UNKNOWN (factual execution failure)
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from asha.adapters.base import ExecutionAdapter
from asha.common import paths as common_paths
from asha.contracts.execution import ExecutionManifest, SemanticFacts

_TEST_RUNNER_RE = re.compile(r"pytest")
_LINT_RUNNER_RE = re.compile(r"ruff|flake8")
_FAILED_COUNT_RE = re.compile(r"(\d+)\s+failed")
_LINT_COUNT_RE = re.compile(r"Found (\d+) errors")


class NativeAdapter(ExecutionAdapter):
    """Execute manifest commands with a native subprocess."""

    def execute(self, manifest: ExecutionManifest) -> SemanticFacts:
        logs = self._capture_logs(manifest)
        exit_codes: dict[str, int] = {}
        test_collection = "UNKNOWN"
        test_execution = "UNKNOWN"
        failed_test_count = 0
        lint_result = "UNKNOWN"
        lint_violations_count = 0

        for idx, command in enumerate(manifest.commands):
            key = f"cmd{idx}"
            env = os.environ.copy()
            env.update(manifest.env_overrides)
            try:
                proc = subprocess.run(
                    command,
                    cwd=manifest.working_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=manifest.timeout_seconds,
                    env=env,
                )
                rc = proc.returncode
                output = (proc.stdout or "") + (proc.stderr or "")
                self._write_log(logs, f"cmd{idx}.log", output)
            except subprocess.TimeoutExpired:
                exit_codes[key] = -1
                continue
            except (OSError, ValueError) as exc:
                exit_codes[key] = -1
                self._write_log(logs, f"cmd{idx}.log", str(exc))
                continue

            exit_codes[key] = rc
            toks = [os.path.basename(t) for t in command]
            joined = " ".join(toks)
            if _TEST_RUNNER_RE.search(joined):
                test_collection, test_execution = self._normalize_test(
                    rc, output)
                failed_test_count = self._failed_count(output)
            elif _LINT_RUNNER_RE.search(joined):
                lint_result = "CLEAN" if rc == 0 else "VIOLATIONS"
                lint_violations_count = self._lint_count(output)
            else:
                # unclassifiable command -> UNKNOWN facts (never guess)
                pass

        raw_logs_ref = str(logs) if logs.exists() else None
        return SemanticFacts(
            run_id=manifest.run_id,
            exit_codes=exit_codes,
            test_collection=test_collection,  # type: ignore[arg-type]
            test_execution=test_execution,  # type: ignore[arg-type]
            failed_test_count=failed_test_count,
            lint_result=lint_result,  # type: ignore[arg-type]
            lint_violations_count=lint_violations_count,
            raw_logs_ref=raw_logs_ref,
        )

    @staticmethod
    def _normalize_test(rc: int, output: str) -> tuple[str, str]:
        if rc == 0:
            return "COLLECTED", "PASSED"
        if rc == 5:
            return "NO_TESTS_COLLECTED", "NOT_RUN"
        return "COLLECTED", "FAILED"

    @staticmethod
    def _failed_count(output: str) -> int:
        m = _FAILED_COUNT_RE.search(output)
        return int(m.group(1)) if m else 0

    @staticmethod
    def _lint_count(output: str) -> int:
        m = _LINT_COUNT_RE.search(output)
        if m:
            return int(m.group(1))
        # ruff/flake8 with violations but no summary line: count risk-rows
        # conservatively; absence of a reliable count stays 0 (not invented).
        return 0

    @staticmethod
    def _capture_logs(manifest: ExecutionManifest) -> Path:
        """External scratch dir for raw logs (Phase D Zone 3)."""
        return common_paths.get_scratch_dir(manifest.run_id)

    @staticmethod
    def _write_log(logs: Path, name: str, content: str) -> None:
        try:
            logs.mkdir(parents=True, exist_ok=True)
            (logs / name).write_text(content, encoding="utf-8")
        except OSError:
            pass  # best-effort log capture; facts remain valid