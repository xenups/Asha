"""Delta Check: distinguish pre-existing verification failures from
newly introduced ones (Phase K.3).

Data model
----------
For a check producing failures:

    BASELINE = failures known before worker execution (same repo, service
               environment, check command, configuration, relevant scope)
    CURRENT  = failures after worker execution
    DELTA    = CURRENT - BASELINE

Failure identity is structural (not raw stdout): pytest nodeids from
``FAILED path::nodeid`` summary lines; ruff/mypy diagnostics from
``path:line:col: CODE message`` lines. Full error messages are NOT part
of the identity (they contain volatile content).

Unknown is never safe: a missing/unreliable baseline means every current
failure is treated as new (fail closed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# pytest -q --tb=line emits:  FAILED tests/a.py::test_x - AssertionError: ...
_PYTEST_FAILED_RE = re.compile(
    r"\bFAILED\s+([^\s]+?)(?:\s+-\s+.*)?$", re.MULTILINE
)
# ruff / the type checker both emit `path:line:col` diagnostics
_DIAG_RE = re.compile(
    r"^([^:]+):(\d+):(\d+):\s*(?:error|warning)?\s*"
    r"((?:[A-Z][A-Z0-9]*)?)\s*:?\s*(.*)$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class FailureIdentity:
    """Stable identity of one verification failure."""

    check: str            # pytest | ruff | mypy
    location: str         # nodeid or path:line:col
    code: str | None      # ruff/mypy rule code, else None
    message: str | None   # first line of message; None when absent

    def to_dict(self) -> dict[str, object]:
        return {
            "check": self.check,
            "location": self.location,
            "code": self.code,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> FailureIdentity:
        return cls(
            check=str(raw["check"]),
            location=str(raw["location"]),
            code=str(raw.get("code")) if raw.get("code") is not None else None,
            message=(str(raw.get("message"))
                     if raw.get("message") is not None else None),
        )


def extract_failures(checks: list[dict]) -> frozenset[FailureIdentity]:
    """Extract stable failure identities from check_runner results.

    Only entries with ``status == "failed"`` contribute. Entries whose
    output cannot be parsed produce NO identity: the delta layer treats
    the run as unknown rather than guessing (caller decides).
    """
    out: set[FailureIdentity] = set()
    for entry in checks:
        if entry.get("status") != "failed":
            continue
        name = entry.get("name", "")
        tail = entry.get("output_tail") or ""
        if name == "pytest":
            for nodeid in _PYTEST_FAILED_RE.findall(tail):
                nodeid = nodeid.strip()
                if nodeid:
                    out.add(FailureIdentity(name, nodeid, None, None))
        elif name in ("ruff", "mypy"):
            for m in _DIAG_RE.finditer(tail):
                raw_path, line, col, code, message = m.groups()
                loc = f"{raw_path}:{line}:{col}"
                out.add(FailureIdentity(
                    name, loc, code or None,
                    (message or "").strip()[:200] or None))
    return frozenset(out)


def has_failures(checks: list[dict]) -> bool:
    return any(e.get("status") == "failed" for e in checks)


def delta_failures(
    current: frozenset[FailureIdentity],
    baseline: frozenset[FailureIdentity] | None,
) -> frozenset[FailureIdentity]:
    """New failures introduced by the worker.

    ``baseline is None`` (unavailable/unreliable) returns ALL current
    failures: unknown is never treated as safe.
    """
    if baseline is None:
        return current
    return current - baseline


def verdict_for(
    current: frozenset[FailureIdentity],
    baseline: frozenset[FailureIdentity] | None,
    *,
    has_failed_checks: bool = False,
) -> str:
    """Governance verdict: NO_FAILURES | PRE_EXISTING_ONLY | NEW_FAILURES
    | UNKNOWN. Fail closed whenever the baseline is unavailable and the
    current run has failures.

    ``has_failed_checks`` must be True when any check entry reported a
    failed status but produced no parseable failure identity: that is an
    UNKNOWN (the failures are real but not attributable), never a clean
    NO_FAILURES -- unparseable must not disappear from the delta."""
    if not current and not has_failed_checks:
        return "NO_FAILURES"
    if baseline is None:
        return "UNKNOWN"
    if not current:
        # failed checks whose identities could not be parsed: not
        # attributable, therefore unknown
        return "UNKNOWN"
    new = current - baseline
    if new:
        return "NEW_FAILURES"
    return "PRE_EXISTING_ONLY"