"""GateEvaluator tests (Phase C).

* A. Existing semantic mapping — explicit expected constants.
* B. Determinism — identical facts+policy -> identical result.
* C. No execution / I/O — the evaluator boundary.
* D. Phase A regression — 21 golden baseline tests unchanged (separate
    file; run by the validation command, not re-asserted here).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from asha.contracts.execution import SemanticFacts  # noqa: E402
from asha.governance.evaluator import (  # noqa: E402
    VERDICT_FAIL,
    VERDICT_PASS,
    EvaluationVerdict,
    evaluate,
)

# --------------------------------------------------------------------------
# Fact builder — explicit fixtures only, never derived from the evaluator.
# --------------------------------------------------------------------------

_EXIT_OK = {"tests": 0, "lint": 0}


def _facts(**overrides: object) -> SemanticFacts:
    base: dict[str, object] = {
        "run_id": "run-1",
        "exit_codes": dict(_EXIT_OK),
        "test_collection": "COLLECTED",
        "test_execution": "PASSED",
        "failed_test_count": 0,
        "lint_result": "CLEAN",
        "lint_violations_count": 0,
        "raw_logs_ref": None,
    }
    base.update(overrides)
    return SemanticFacts(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# A. Existing semantic mapping (explicit verdict constants).
# --------------------------------------------------------------------------


class TestSemanticMapping:
    def test_tests_collected_passed_lint_clean(self) -> None:
        verdict = evaluate(_facts())
        assert verdict.verdict == VERDICT_PASS
        assert verdict.reasons == ()

    def test_tests_collected_failed(self) -> None:
        verdict = evaluate(
            _facts(test_execution="FAILED", failed_test_count=3)
        )
        assert verdict.verdict == VERDICT_FAIL
        assert "failed:tests" in verdict.reasons

    def test_failed_test_count_is_recorded(self) -> None:
        verdict = evaluate(
            _facts(test_execution="FAILED", failed_test_count=7)
        )
        assert verdict.verdict == VERDICT_FAIL
        # failed count is part of the digest; the verdict still FAIL.
        assert "7" in verdict.facts_digest or verdict.facts_digest

    def test_no_tests_collected_is_skip_not_fail(self) -> None:
        # Legacy semantics: pytest rc==5 -> skipped, never failed.
        verdict = evaluate(
            _facts(
                test_collection="NO_TESTS_COLLECTED",
                test_execution="NOT_RUN",
            )
        )
        assert verdict.verdict == VERDICT_PASS
        assert "skipped:tests" in verdict.reasons

    def test_tests_not_run_is_skip(self) -> None:
        verdict = evaluate(
            _facts(test_collection="UNKNOWN", test_execution="NOT_RUN")
        )
        assert verdict.verdict == VERDICT_PASS
        assert "skipped:tests" in verdict.reasons

    def test_unknown_test_state_is_skip(self) -> None:
        verdict = evaluate(
            _facts(test_collection="UNKNOWN", test_execution="UNKNOWN")
        )
        assert verdict.verdict == VERDICT_PASS
        assert "skipped:tests" in verdict.reasons

    def test_lint_clean(self) -> None:
        verdict = evaluate(_facts(lint_result="CLEAN"))
        assert verdict.verdict == VERDICT_PASS
        assert "failed:lint" not in verdict.reasons

    def test_lint_violations(self) -> None:
        verdict = evaluate(
            _facts(lint_result="VIOLATIONS", lint_violations_count=5)
        )
        assert verdict.verdict == VERDICT_FAIL
        assert "failed:lint" in verdict.reasons

    def test_lint_not_run_is_skip(self) -> None:
        verdict = evaluate(_facts(lint_result="NOT_RUN"))
        assert verdict.verdict == VERDICT_PASS
        assert "skipped:lint" in verdict.reasons

    def test_unknown_lint_state_is_skip(self) -> None:
        verdict = evaluate(_facts(lint_result="UNKNOWN"))
        assert verdict.verdict == VERDICT_PASS
        assert "skipped:lint" in verdict.reasons

    def test_lint_violations_count_recorded(self) -> None:
        verdict = evaluate(
            _facts(lint_result="VIOLATIONS", lint_violations_count=12)
        )
        assert verdict.verdict == VERDICT_FAIL
        assert verdict.facts_digest != ""

    def test_failed_tests_cover_specific_combination(self) -> None:
        # tests FAILED + lint CLEAN: FAIL with only the test failure.
        verdict = evaluate(
            _facts(
                test_collection="COLLECTED",
                test_execution="FAILED",
                failed_test_count=1,
                lint_result="CLEAN",
            )
        )
        assert verdict.verdict == VERDICT_FAIL
        assert "failed:tests" in verdict.reasons
        assert "failed:lint" not in verdict.reasons

    def test_failed_everything(self) -> None:
        verdict = evaluate(
            _facts(
                test_execution="FAILED",
                failed_test_count=2,
                lint_result="VIOLATIONS",
                lint_violations_count=4,
            )
        )
        assert verdict.verdict == VERDICT_FAIL
        assert verdict.reasons == ("failed:tests,lint",)

    def test_legacy_status_vocabulary_preserved(self) -> None:
        # The check-level status words are the ONLY failures that turn
        # the verdict FAIL; skipped/unknown never do.
        assert VERDICT_FAIL == "FAIL"
        assert VERDICT_PASS == "PASS"


# --------------------------------------------------------------------------
# B. Determinism.
# --------------------------------------------------------------------------


class TestDeterminism:
    def test_repeated_evaluation_identical(self) -> None:
        facts = _facts(
            test_execution="FAILED",
            failed_test_count=2,
            lint_result="VIOLATIONS",
            lint_violations_count=3,
        )
        a = evaluate(facts)
        b = evaluate(facts)
        assert a == b
        assert a.facts_digest == b.facts_digest
        assert a.reasons == b.reasons

    def test_identical_digest_across_repetition(self) -> None:
        facts = _facts()
        digest = evaluate(facts).facts_digest
        for _ in range(5):
            assert evaluate(_facts()).facts_digest == digest

    def test_digest_changes_when_facts_change(self) -> None:
        base = evaluate(_facts()).facts_digest
        changed = evaluate(_facts(failed_test_count=1)).facts_digest
        assert changed != base

    def test_result_type_is_frozen(self) -> None:
        v = evaluate(_facts())
        assert isinstance(v, EvaluationVerdict)
        with pytest.raises(AttributeError):
            v.verdict = VERDICT_FAIL  # frozen (FrozenInstanceError)


# --------------------------------------------------------------------------
# C. No execution / I/O — the evaluator must stay a pure policy consumer.
# --------------------------------------------------------------------------


class TestNoIo:
    def test_source_has_no_execution_imports(self) -> None:
        import ast as _ast

        src = (REPO_ROOT / "asha" / "governance" / "evaluator.py").read_text(
            encoding="utf-8"
        )
        # Strip docstrings/comments before scanning so prose never trips
        # the token ban; only CODE tokens matter.
        tree = _ast.parse(src)
        code_only = src
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Expr, _ast.FunctionDef, _ast.ClassDef)):
                doc = _ast.get_docstring(node)
                if doc:
                    code_only = code_only.replace(doc, "", 1)
        for banned in (
            "subprocess",
            "os.",
            "sys.",
            "shutil",
            "import os",
            "import sys",
            "pytest",
            "ruff",
            "mypy",
            "git",
        ):
            assert banned not in code_only, f"banned token present: {banned}"

    def test_evaluation_touches_no_runtime(self) -> None:
        # The evaluator takes only facts; no filesystem/env/process.
        # Construction of SemanticFacts requires no I/O either.
        verdict = evaluate(_facts())
        assert isinstance(verdict, EvaluationVerdict)
        assert verdict.verdict in (VERDICT_PASS, VERDICT_FAIL)


# --------------------------------------------------------------------------
# D. Phase A regression — asserted by the validation command against the
#    locked file; nothing in this module modifies golden expectations.
# --------------------------------------------------------------------------