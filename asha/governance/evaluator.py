"""GateEvaluator — policy consumer for SemanticFacts (Phase C).

Pure extraction of the EXISTING governance evaluation semantics from the
legacy `check_runner` result records (status: passed/failed/skipped) into
a facts-only evaluator. No execution, no I/O, no runtime objects.

Mapping (preserved byte-for-byte from the legacy semantics):

* check status vocabulary: passed / failed / skipped
* pytest exit code 5  ->  SKIP (never failed, never silent)   [note: no_tests_collected]
* empty target set    ->  SKIP                                [note: empty_target_set_proven]
* NOT_RUN / UNKNOWN  ->  SKIP (never failed, never silent)
* any check FAILED   ->  verdict FAIL, reasons carry the failed check names
* no check FAILED    ->  verdict PASS (skips never fail the gate)

facts_digest: canonical deterministic serialization with stable field
ordering. No process-dependent or unordered representations.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from asha.contracts.execution import SemanticFacts

# Public verdict interface for the governance chain.
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"

# Facts-only check names (the evaluator's vocabulary of policy-relevant
# facts; NOT execution identifiers).
CHECK_TESTS = "tests"
CHECK_LINT = "lint"

# Verdict vocabulary — bounded to the existing governance contract
# (PASS/FAIL plus the check-level skipped/relative facts). No new
# semantic states are invented here.
Verdict = Literal["PASS", "FAIL"]

# Status vocabulary mirrors the legacy check statuses exactly.
_LEGACY_FAILED = "failed"
_LEGACY_SKIPPED = "skipped"


@dataclass(frozen=True)
class EvaluationVerdict:
    """Strongly typed result of a facts-only gate evaluation.

    Vocabulary matches the existing governance contract:
    verdict ∈ {PASS, FAIL}; reasons carry the failed/skipped check names;
    facts_digest is the SHA-256 of the canonical facts serialization.
    """

    verdict: Verdict
    reasons: tuple[str, ...] = ()
    facts_digest: str = ""


def _canonical_facts(facts: SemanticFacts) -> str:
    """Canonical deterministic serialization (stable field order)."""
    return "|".join(
        (
            facts.run_id,
            facts.test_collection,
            facts.test_execution,
            str(facts.failed_test_count),
            facts.lint_result,
            str(facts.lint_violations_count),
            facts.raw_logs_ref or "",
            "".join(f"{k}={v};" for k, v in sorted(facts.exit_codes.items())),
        )
    )


def _facts_digest(facts: SemanticFacts) -> str:
    return sha256(_canonical_facts(facts).encode("utf-8")).hexdigest()


def _test_status(facts: SemanticFacts) -> str:
    """Map test-execution facts onto the legacy check status vocabulary.

    Rule (preserved semantics): pytest exit code 5 (NO_TESTS_COLLECTED)
    and NOT_RUN/UNKNOWN are SKIPPED, never failed and never silent.
    """
    if facts.test_execution == "FAILED":
        return _LEGACY_FAILED
    if facts.test_execution == "PASSED":
        return "passed"
    # NOT_RUN / UNKNOWN / NO_TESTS_COLLECTED -> skipped (legacy rc==5 rule)
    return _LEGACY_SKIPPED


def _lint_status(facts: SemanticFacts) -> str:
    if facts.lint_result == "VIOLATIONS":
        return _LEGACY_FAILED
    if facts.lint_result == "CLEAN":
        return "passed"
    return _LEGACY_SKIPPED


def evaluate(
    facts: SemanticFacts,
    *,
    policy: dict[str, object] | None = None,
) -> EvaluationVerdict:
    """Evaluate already-produced facts into a governance verdict.

    Facts-only ingestion: no filesystem, environment, process, git, or
    runtime objects are touched. Deterministic: identical facts +
    identical policy -> identical verdict/reasons/digest.
    """
    # Immutable policy configuration (currently unused; the existing
    # semantics need none). Kept explicit so the adapter boundary has a
    # stable seam for later policy phases without touching facts.
    _ = policy or {}

    failed: list[str] = []
    skipped: list[str] = []

    test_status = _test_status(facts)
    if test_status == _LEGACY_FAILED:
        failed.append(CHECK_TESTS)
    elif test_status == _LEGACY_SKIPPED:
        skipped.append(CHECK_TESTS)

    lint_status = _lint_status(facts)
    if lint_status == _LEGACY_FAILED:
        failed.append(CHECK_LINT)
    elif lint_status == _LEGACY_SKIPPED:
        skipped.append(CHECK_LINT)

    reasons: list[str] = []
    if failed:
        reasons.append("failed:" + ",".join(failed))
    if skipped:
        reasons.append("skipped:" + ",".join(skipped))

    verdict: Verdict = VERDICT_FAIL if failed else VERDICT_PASS
    return EvaluationVerdict(
        verdict=verdict,
        reasons=tuple(reasons),
        facts_digest=_facts_digest(facts),
    )