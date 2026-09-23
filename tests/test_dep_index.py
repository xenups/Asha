"""Unit tests for the Phase 2 dependency fact index (stdlib ast only).

TDD: these tests are written BEFORE dep_index.py exists -- their first run
must fail with ModuleNotFoundError, demonstrating the pre-implementation
failure required by the Phase 2 execution order.
"""
from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / ".hermes" / "tools"
sys.path.insert(0, str(TOOLS))

import dep_index


def test_import_extraction_basic() -> None:
    index = dep_index.DependencyIndex()
    facts = index.analyze(
        "pkg/a.py",
        "import os\nimport os.path\nfrom pkg import leaf\n",
    )
    imports = {(fact.target, fact.confidence, fact.location)
               for fact in facts if fact.relation == "imports"}
    assert ("os", "CERTAIN", "pkg/a.py:1") in imports
    assert ("os.path", "CERTAIN", "pkg/a.py:2") in imports
    assert ("pkg", "CERTAIN", "pkg/a.py:3") in imports
    # Only file-level imports/calls/defs semantics: no confidence fuzziness.
    assert all(fact.confidence in ("CERTAIN", "UNCERTAIN")
               for fact in facts)


def test_relative_import_extraction() -> None:
    index = dep_index.DependencyIndex()
    facts = index.analyze(
        "pkg/a.py",
        "from . import leaf\nfrom .sub import thing\n",
    )
    targets = {fact.target for fact in facts
               if fact.relation == "imports"}
    # level dots preserved so the graph builder can resolve relatively.
    assert targets == {".leaf", ".sub"}


def test_calls_extraction_with_location() -> None:
    index = dep_index.DependencyIndex()
    facts = index.analyze(
        "pkg/a.py",
        "def run():\n    helper(1)\n    obj.go()\n",
    )
    calls = {(fact.target, fact.location)
             for fact in facts if fact.relation == "calls"}
    assert calls == {("helper", "pkg/a.py:2"),
                     ("obj.go", "pkg/a.py:3")}
    assert all(fact.confidence == "CERTAIN" for fact in facts)


def test_cache_skips_unchanged_file() -> None:
    index = dep_index.DependencyIndex()
    content = "import os\n"
    index.analyze("x.py", content)
    assert index.parse_count == 1
    # Identical (path, sha256) must never reparse.
    index.analyze("x.py", content)
    assert index.parse_count == 1
    # Changed content parses exactly once more.
    index.analyze("x.py", "import sys\n")
    assert index.parse_count == 2
    assert "x.py" in index.known_paths()


def test_dynamic_import_marks_uncertain() -> None:
    index = dep_index.DependencyIndex()
    facts = index.analyze(
        "x.py",
        "importlib.import_module('m')\n__import__('n')\n",
    )
    uncertain = [fact for fact in facts
                 if fact.confidence == "UNCERTAIN"]
    assert {fact.target for fact in uncertain} == {"m", "n"}
    assert all(fact.relation == "imports" for fact in uncertain)
    # Non-constant target: still UNCERTAIN, never an invented name.
    facts = index.analyze("y.py", "importlib.import_module(name)\n")
    assert any(fact.target == "*" and fact.confidence == "UNCERTAIN"
               for fact in facts)


def test_parse_failure_marks_uncertain() -> None:
    index = dep_index.DependencyIndex()
    facts = index.analyze("bad.py", "def broken(:\n")
    assert len(facts) == 1
    fact = facts[0]
    assert fact.source == "bad.py"
    assert fact.confidence == "UNCERTAIN"
    assert fact.relation == "depends_on"
    assert fact.target == "*"
    # Failures are cached too: identical broken content is not reparsed.
    before = index.parse_count
    index.analyze("bad.py", "def broken(:\n")
    assert index.parse_count == before
