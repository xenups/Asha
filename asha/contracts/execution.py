"""Execution boundary contracts (Phase B): data-only transport types.

Separates governance from execution. These are pure data definitions:

* NO subprocess, git, pytest/ruff/mypy, AST, classification, verdicts.
* The governance core must NOT interpret ``commands`` — the
  adapter/executor boundary owns execution mechanics.
* Facts are execution facts, NEVER governance verdicts
  (no PASS/FAIL/SKIP/SHIP/REJECT/HOLD/UNRELIABLE_BASE here).

The mapping of facts to policy outcomes belongs to governance policy,
not to this contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

TestCollection = Literal[
    "COLLECTED",
    "NO_TESTS_COLLECTED",
    "UNKNOWN",
]

TestExecution = Literal[
    "PASSED",
    "FAILED",
    "NOT_RUN",
    "UNKNOWN",
]

LintResult = Literal[
    "CLEAN",
    "VIOLATIONS",
    "NOT_RUN",
    "UNKNOWN",
]


class ExecutionManifest:
    """Minimum transport contract to REQUEST evaluation.

    ``commands`` are opaque execution instructions: a list of argv
    sequences the executor/adaptor understands. The governance core
    must not execute or interpret them.
    """

    __slots__ = (
        "commands",
        "env_overrides",
        "run_id",
        "target_root",
        "timeout_seconds",
        "working_dir",
    )

    def __init__(
        self,
        *,
        run_id: str,
        target_root: Path,
        commands: list[list[str]],
        env_overrides: dict[str, str],
        working_dir: Path,
        timeout_seconds: int,
    ) -> None:
        self.run_id = run_id
        self.target_root = target_root
        self.commands = commands
        self.env_overrides = env_overrides
        self.working_dir = working_dir
        self.timeout_seconds = timeout_seconds


class SemanticFacts:
    """Facts returned by an execution adapter.

    Facts only — no policy outcomes. The vocabulary is deliberately
    disjoint from governance verdicts: NO_TESTS_COLLECTED is a fact;
    whether it becomes SKIP/FAIL/HOLD is a policy decision outside
    this contract.
    """

    __slots__ = (
        "exit_codes",
        "failed_test_count",
        "lint_result",
        "lint_violations_count",
        "raw_logs_ref",
        "run_id",
        "test_collection",
        "test_execution",
    )

    def __init__(
        self,
        *,
        run_id: str,
        exit_codes: dict[str, int],
        test_collection: TestCollection,
        test_execution: TestExecution,
        failed_test_count: int,
        lint_result: LintResult,
        lint_violations_count: int,
        raw_logs_ref: str | None,
    ) -> None:
        self.run_id = run_id
        self.exit_codes = exit_codes
        self.test_collection = test_collection
        self.test_execution = test_execution
        self.failed_test_count = failed_test_count
        self.lint_result = lint_result
        self.lint_violations_count = lint_violations_count
        self.raw_logs_ref = raw_logs_ref