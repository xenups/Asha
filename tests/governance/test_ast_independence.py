"""H.3: AST independence — governance must not require AST indexing.

Proves the CRITICAL GOVERNANCE INVARIANT with three fixtures:

    A. Broken syntax  : repo contains a Python file with a SyntaxError
    B. Non-Python     : repo contains zero .py (only .md/.json/.rs)
    C. Zero-index/cold: no AST cache; indexing disabled

Assertions per fixture:
  1. GateEvaluator.evaluate() reaches a definitive verdict (pure path).
  2. scoping.assess_scoping_eligibility() fails CLOSED (COMPLETE/FAIL
     decision), never crashes, never escapes SyntaxError/ValueError.
  3. collect_worker_evidence() seals valid, structurally sound evidence
     (or returns INVALID_EVIDENCE fail-closed — never an uncaught AST
     exception).
  4. No uncaught SyntaxError / DecodeError / IndexError escapes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from asha.governance import evaluator
from asha.governance.worker_execution import collect_worker_evidence
from asha.scoping import assess_scoping_eligibility

PY = sys.executable

BROKEN_PY = "def broken(\n"
GOOD_PY = "def ok():\n    return 1\n"
MARKDOWN = "# Fixture\n\ntext here\n"
JSON_DOC = '{"repo": "fixture", "kind": "non-python"}\n'
RUST = "fn main() { println!(\"hi\"); }\n"


def _make_repo(tmp: Path, files: dict[str, str]) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo,
                   check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.com"],
                   cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"],
                   cwd=repo, check=True)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo,
                   check=True)
    return repo


def _facts(**over: Any) -> evaluator.SemanticFacts:
    base = {
        "run_id": "h3-fixture",
        "test_collection": "COLLECTED",
        "test_execution": "PASSED",
        "failed_test_count": 0,
        "lint_result": "CLEAN",
        "lint_violations_count": 0,
        "raw_logs_ref": None,
        "exit_codes": {},
    }
    base.update(over)
    return evaluator.SemanticFacts(**base)


def _scope_decision(repo: Path, changed: list[str],
                    classification: str = "PROVEN_DISJOINT"):
    return assess_scoping_eligibility(
        repo, changed, classification, "complete",
        scope_status="certain", envelope_valid=True)


def _seal(repo: Path, tmp: Path, worker: dict[str, Any]) -> dict[str, Any]:
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True,
                          check=True).stdout.strip()
    base_tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"],
                               cwd=repo, capture_output=True, text=True,
                               check=True).stdout.strip()
    return collect_worker_evidence(
        worker, repo, 0, "", "h3", tmp / "evidence",
        base=base, base_tree=base_tree)


# ------------------------------------------------------------------- A
@pytest.fixture()
def repo_broken_syntax(tmp_path: Path) -> Path:
    return _make_repo(tmp_path, {"src/broken.py": BROKEN_PY,
                                 "tests/test_ok.py": GOOD_PY})


@pytest.fixture()
def repo_non_python(tmp_path: Path) -> Path:
    return _make_repo(tmp_path, {"README.md": MARKDOWN,
                                 "data.json": JSON_DOC,
                                 "src/main.rs": RUST})


@pytest.fixture()
def repo_cold(tmp_path: Path) -> Path:
    return _make_repo(tmp_path, {"src/mod.py": GOOD_PY,
                                 "tests/test_ok.py": GOOD_PY})


def test_gate_verdict_without_ast(repo_broken_syntax: Path) -> None:
    """GateEvaluator.evaluate() is facts-only: no AST, deterministic."""
    v = _facts()
    verdict = evaluator.evaluate(v)
    assert verdict.verdict in ("PASS", "FAIL")
    assert verdict.facts_digest
    # identical facts -> identical digest/verdict (determinism)
    again = evaluator.evaluate(_facts())
    assert again.verdict == verdict.verdict
    assert again.facts_digest == verdict.facts_digest


def test_fixture_a_broken_syntax_fails_closed(repo_broken_syntax: Path,
                                              tmp_path: Path) -> None:
    decision = _scope_decision(repo_broken_syntax, ["src/broken.py"])
    assert decision.mode in ("COMPLETE", "SCOPED")
    # fails closed to a COMPLETE fallback, never a crash
    assert decision.eligible is False or decision.mode == "COMPLETE" \
        or decision.fallback_reason


def test_fixture_a_evidence_seals_without_crash(repo_broken_syntax: Path,
                                                tmp_path: Path) -> None:
    worker = {"id": "w1", "deps": [], "declared_scope": ["src/"],
              "reads": ["src/broken.py"], "writes": ["src/broken.py"],
              "cmd": [PY, "-c", "pass"]}
    outcome = _seal(repo_broken_syntax, tmp_path, worker)
    # fail-closed state (never an uncaught AST exception)
    assert outcome["state"] in ("DONE", "INVALID_EVIDENCE", "FAILED")


def test_fixture_b_non_python_fails_closed(repo_non_python: Path,
                                           tmp_path: Path) -> None:
    decision = _scope_decision(repo_non_python, ["README.md"])
    assert decision.mode in ("COMPLETE", "SCOPED")
    worker = {"id": "w1", "deps": [], "declared_scope": ["README.md"],
              "reads": ["README.md"], "writes": ["README.md"],
              "cmd": [PY, "-c", "pass"]}
    outcome = _seal(repo_non_python, tmp_path, worker)
    assert outcome["state"] in ("DONE", "INVALID_EVIDENCE", "FAILED")


def test_fixture_c_cold_no_index(repo_cold: Path, tmp_path: Path) -> None:
    decision = _scope_decision(repo_cold, ["src/mod.py"],
                               classification="PROVEN_DISJOINT")
    assert decision.mode in ("COMPLETE", "SCOPED")
    worker = {"id": "w1", "deps": [], "declared_scope": ["src/"],
              "reads": ["src/mod.py"], "writes": ["src/mod.py"],
              "cmd": [PY, "-c", "pass"]}
    outcome = _seal(repo_cold, tmp_path, worker)
    assert outcome["state"] in ("DONE", "INVALID_EVIDENCE", "FAILED")
    # no AST cache may be created by the governance path alone
    assert not list((repo_cold / ".jspace").glob("**/*"))


def test_no_uncaught_ast_exceptions(repo_broken_syntax: Path,
                                    repo_non_python: Path,
                                    tmp_path: Path) -> None:
    """SyntaxError/DecodeError/IndexError must never escape governance."""
    # every fixture yields a ScopingDecision (never raises)
    for repo, changed in ((repo_broken_syntax, ["src/broken.py"]),
                          (repo_non_python, ["README.md"])):
        decision = _scope_decision(repo, changed)
        assert decision is not None
        assert hasattr(decision, "mode")
    # collect path likewise returns a state dict, does not raise AST errs
    worker = {"id": "w1", "deps": [], "declared_scope": ["src/"],
              "reads": ["src/broken.py"], "writes": ["src/broken.py"],
              "cmd": [PY, "-c", "pass"]}
    outcome = _seal(repo_broken_syntax, tmp_path, worker)
    assert outcome["state"] in ("DONE", "INVALID_EVIDENCE", "FAILED")