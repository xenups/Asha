"""CIAdapter — consumes externally produced execution evidence (Phase F).

Provider-neutral. Ingests deterministic report formats (JUnit XML for
tests, ruff/flake8 plain output for lint). Produces VERIFIED facts only:

* provenance checks: commit_sha, head/base binding, artifact identity
  and integrity must be supplied in the report; a missing/corrupt/
  mismatched report becomes UNKNOWN, never PASSED or another optimistic
  fact.
* No policy vocabulary (PASS/FAIL/SHIP) — facts only.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from asha.adapters.base import ExecutionAdapter
from asha.contracts.execution import ExecutionManifest, SemanticFacts

_FAILED_COUNT_RE = re.compile(r"(\d+)\s+failed")
_LINT_COUNT_RE = re.compile(r"Found (\d+) errors")


class CIAdapter(ExecutionAdapter):
    """Ingest a CI-produced report bundle into verified facts.

    manifest.commands is ignored (CI already ran); the manifest
    identifies the run and provides the report location via
    env_overrides: {"ASHA_CI_REPORT": "<dir>"} containing:
        junit.xml        (test report)
        lint.txt         (ruff/flake8 output)
        provenance.json  (commit_sha, head_sha, base_sha, workflow_id)
    """

    def execute(self, manifest: ExecutionManifest) -> SemanticFacts:
        report_dir = manifest.env_overrides.get("ASHA_CI_REPORT")
        if not report_dir:
            return self._unknown(manifest, "no CI report dir configured")
        base = Path(report_dir)
        if not base.is_dir():
            return self._unknown(manifest, "report dir missing")

        provenance = self._read_provenance(base)
        if provenance is None:
            return self._unknown(manifest, "provenance missing/unreadable")
        if not self._provenance_consistent(manifest, provenance):
            return self._unknown(manifest, "provenance mismatch")

        junit = base / "junit.xml"
        lint = base / "lint.txt"
        test_collection = "UNKNOWN"
        test_execution = "UNKNOWN"
        failed_tests = 0
        lint_result = "UNKNOWN"
        lint_violations = 0

        if junit.is_file():
            parsed = self._parse_junit(junit)
            if parsed is None:
                return self._unknown(manifest, "junit malformed")
            collected, passed, failed = parsed
            test_collection = "COLLECTED" if collected else "NO_TESTS_COLLECTED"
            test_execution = "PASSED" if failed == 0 and collected else (
                "FAILED" if failed > 0 else "NOT_RUN")
            failed_tests = failed
        else:
            return self._unknown(manifest, "junit report missing")

        if lint.is_file():
            text = lint.read_text(encoding="utf-8", errors="replace")
            if "All checks passed" in text:
                lint_result = "CLEAN"
                lint_violations = 0
            elif _LINT_COUNT_RE.search(text):
                lint_result = "VIOLATIONS"
                m = _LINT_COUNT_RE.search(text)
                lint_violations = int(m.group(1)) if m else 0
            else:
                lint_result = "CLEAN" if text.strip() == "" else "VIOLATIONS"

        return SemanticFacts(
            run_id=manifest.run_id,
            exit_codes={},
            test_collection=test_collection,  # type: ignore[arg-type]
            test_execution=test_execution,  # type: ignore[arg-type]
            failed_test_count=failed_tests,
            lint_result=lint_result,  # type: ignore[arg-type]
            lint_violations_count=lint_violations,
            raw_logs_ref=str(base),
        )

    @staticmethod
    def _unknown(manifest: ExecutionManifest, reason: str) -> SemanticFacts:
        return SemanticFacts(
            run_id=manifest.run_id,
            exit_codes={},
            test_collection="UNKNOWN",
            test_execution="UNKNOWN",
            failed_test_count=0,
            lint_result="UNKNOWN",
            lint_violations_count=0,
            raw_logs_ref=reason,
        )

    @staticmethod
    def _read_provenance(base: Path) -> dict | None:
        prov = base / "provenance.json"
        if not prov.is_file():
            return None
        import json
        try:
            data = json.loads(prov.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _provenance_consistent(
        manifest: ExecutionManifest, prov: dict,
    ) -> bool:
        """Head/base binding + identity. Missing -> not consistent."""
        required = ("commit_sha", "head_sha", "base_sha", "workflow_id")
        return all(prov.get(k) for k in required)

    @staticmethod
    def _parse_junit(path: Path) -> tuple[int, int, int] | None:
        """Return (tests, passed, failed) or None for malformed XML."""
        try:
            root = ET.parse(path).getroot()
            if root.tag != "testsuite":
                return None
            tests = int(root.attrib.get("tests", "0"))
            failures = int(root.attrib.get("failures", "0"))
            errors = int(root.attrib.get("errors", "0"))
            passed = tests - failures - errors
            return tests, passed, failures + errors
        except (ET.ParseError, ValueError, OSError):
            return None