"""Why a mandatory check failed: code failure versus environment gap.

A failed check can mean two very different things, and only one of them is
about the worker's change:

* the suite judged the code -- parseable failures, attributable to a delta;
* the *environment* could not run the suite -- a missing dependency, a
  missing tool, a broken collection. The exit code is identical, so the
  exit code alone cannot tell an operator which to fix.

Everything here is a pure classification of an already-captured check
entry (``exit_code``, ``note``, ``output_tail``, ``interpreter``). It
decides nothing, seals nothing and never relaxes a fail-closed rule: a
failed check stays failed, and an unattributable one stays unknown.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ENVIRONMENT_UNRESOLVED = "environment_unresolved"
MISSING_DEPENDENCY = "missing_dependency"
TOOL_UNAVAILABLE = "tool_unavailable"
COLLECTION_ERROR = "collection_error"
TEST_FAILURES = "test_failures"
TIMEOUT = "timeout"
CRASHED = "crashed"
UNKNOWN = "unknown"

#: Kinds the runner could not fix by changing code: the environment is at fault.
ENVIRONMENT_KINDS = (ENVIRONMENT_UNRESOLVED, MISSING_DEPENDENCY,
                     TOOL_UNAVAILABLE)

_MODULE_NOT_FOUND_RE = re.compile(
    r"ModuleNotFoundError: No module named '([^']+)'")
_CANNOT_IMPORT_RE = re.compile(r"ImportError: cannot import name '([^']+)'")
_COLLECTION_RE = re.compile(
    r"error during collection|ERROR collecting|errors? during collection")
_TIMEOUT_RE = re.compile(r"timed out|timeout|deadline", re.IGNORECASE)
_FAILED_LINE_RE = re.compile(r"^FAILED\s+", re.MULTILINE)

_FALLBACK_ENV = "ASHA_ALLOW_SYSTEM_PYTHON_FALLBACK"


@dataclass(frozen=True)
class CheckDiagnosis:
    """What a failed check entry proves about its own failure."""

    kind: str
    check: str
    module: str | None = None
    interpreter: str | None = None
    hermetic: bool | None = None
    service_root: str | None = None

    @property
    def is_environment(self) -> bool:
        return self.kind in ENVIRONMENT_KINDS

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "check": self.check,
            "module": self.module,
            "interpreter": self.interpreter,
            "hermetic": self.hermetic,
            "service_root": self.service_root,
        }

    def hint(self) -> str:
        """One actionable line for the operator (never a code change)."""
        where = self.service_root or "<service root>"
        if self.kind == MISSING_DEPENDENCY:
            return (f"'{self.module}' is not installed for "
                    f"{self.interpreter or 'the check interpreter'}; the "
                    f"project's dependencies belong in a service-local "
                    f"environment ({where}/.venv), not in Asha's own "
                    f"interpreter. Create that environment, or set "
                    f"{_FALLBACK_ENV}=false to refuse before checks run.")
        if self.kind == TOOL_UNAVAILABLE:
            return (f"the {self.check} tool is not installed for "
                    f"{self.interpreter or 'the check interpreter'}; install "
                    f"it in the project environment ({where}/.venv) or set "
                    f"{_FALLBACK_ENV}=false.")
        if self.kind == ENVIRONMENT_UNRESOLVED:
            return ("the service environment could not be resolved, so no "
                    f"check ran for {where}; provision its environment "
                    f"({where}/.venv) and re-run.")
        if self.kind == COLLECTION_ERROR:
            return ("the test suite could not be collected, so no verdict "
                    "exists for the code; fix the collection error before "
                    "reading this run as a test failure.")
        if self.kind == TIMEOUT:
            return ("the check exceeded its budget and was killed; the suite "
                    "never produced a verdict.")
        return ("the failed check produced no parseable failure identity; "
                "treat the result as unknown, never as a pass.")


def _stdlib_names() -> frozenset[str]:
    names = getattr(sys, "stdlib_module_names", None)
    return frozenset(names) if names else frozenset()


def _is_project_local(module: str, root: Path | None) -> bool:
    """A module that lives in the repository is not a missing dependency."""
    if root is None:
        return False
    head = module.split(".")[0]
    for candidate in (root / head, root / "src" / head):
        if candidate.exists():
            return True
    return False


def classify(entry: dict, *, repo_root: Path | None = None
             ) -> CheckDiagnosis | None:
    """Classify one check entry; ``None`` when it did not fail."""
    if str(entry.get("status")) != "failed":
        return None
    check = str(entry.get("name") or "")
    tail = str(entry.get("output_tail") or "")
    note = str(entry.get("note") or "")
    interpreter = str(entry["interpreter"]) if entry.get("interpreter") else None
    hermetic = (bool(entry["hermetic"])
                if entry.get("hermetic") is not None else None)
    service_root = (str(entry["service_root"])
                    if entry.get("service_root") else None)

    def make(kind: str, module: str | None = None) -> CheckDiagnosis:
        return CheckDiagnosis(kind=kind, check=check, module=module,
                              interpreter=interpreter, hermetic=hermetic,
                              service_root=service_root)

    if entry.get("failure_mode") == ENVIRONMENT_UNRESOLVED:
        return make(ENVIRONMENT_UNRESOLVED)
    if entry.get("timed_out") or _TIMEOUT_RE.search(note):
        return make(TIMEOUT)
    if not entry.get("launched", True) or "launch failed" in note:
        return make(CRASHED)
    missing = _MODULE_NOT_FOUND_RE.search(tail)
    if missing:
        module = missing.group(1)
        if _is_project_local(module, repo_root) or \
                module.split(".")[0] in _stdlib_names():
            return make(COLLECTION_ERROR)
        return make(MISSING_DEPENDENCY, module)
    if check in ("ruff", "mypy") and "No module named" in tail:
        return make(TOOL_UNAVAILABLE)
    if _CANNOT_IMPORT_RE.search(tail) or _COLLECTION_RE.search(tail):
        return make(COLLECTION_ERROR)
    if _FAILED_LINE_RE.search(tail):
        return make(TEST_FAILURES)
    return make(UNKNOWN)


def first_environment_gap(checks: list[dict] | None,
                          *, repo_root: Path | None = None
                          ) -> CheckDiagnosis | None:
    """The first failure that is an environment gap, not a code failure."""
    for entry in checks or []:
        found = classify(entry, repo_root=repo_root)
        if found is not None and found.is_environment:
            return found
    return None


def describe(checks: list[dict] | None,
             *, repo_root: Path | None = None) -> list[dict[str, object]]:
    """Diagnoses for every failed entry (evidence payload)."""
    out: list[dict[str, object]] = []
    for entry in checks or []:
        found = classify(entry, repo_root=repo_root)
        if found is not None:
            out.append(found.to_dict())
    return out
