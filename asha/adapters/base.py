"""Execution adapter boundary (Phase F).

Thin transport contract between governance and execution environments.

    ExecutionManifest
        ↓
    ExecutionAdapter
        ↓
    SemanticFacts
        ↓
    GateEvaluator
        ↓
    EvaluationVerdict

The adapter EXECUTES or INGESTS; it never decides policy
(PASS/FAIL/SKIP/SHIP are GateEvaluator vocabulary).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from asha.contracts.execution import ExecutionManifest, SemanticFacts


class ExecutionAdapter(ABC):
    """Smallest adapter protocol: manifest in, facts out."""

    @abstractmethod
    def execute(self, manifest: ExecutionManifest) -> SemanticFacts:
        """Transport/execute the manifest; return factual results only."""
        raise NotImplementedError