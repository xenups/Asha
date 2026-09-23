"""Phase: promote orchestrator to top-level asha/ (package tests).

importorskip keeps these tests honest under the engine's isolated-worker
verification: in a pre-migration worktree `asha` does not exist yet, so
they skip (never silently pass, never break collection); after the
governed integration they run against the real package + legacy shims.
"""
import importlib
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / ".hermes" / "tools"
ROOT = Path(__file__).resolve().parents[1]
for entry in (str(TOOLS), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import pytest


def test_package_import_surface() -> None:
    asha = pytest.importorskip("asha")
    importlib.import_module("asha.scheduler")
    importlib.import_module("asha.runner")
    importlib.import_module("asha.mcp_server")
    scheduler = importlib.import_module("asha.scheduler")
    assert scheduler.GovernedScheduler is asha.GovernedScheduler


def test_legacy_shims_reexport_same_objects() -> None:
    asha = pytest.importorskip("asha")
    orchestrator = pytest.importorskip("orchestrator")
    assert orchestrator.CommandRunner is asha.CommandRunner
    assert orchestrator.GovernedScheduler is asha.GovernedScheduler
    legacy_mcp = importlib.import_module("orchestrator.mcp_server")
    real_mcp = importlib.import_module("asha.mcp_server")
    assert legacy_mcp.handle_message is real_mcp.handle_message
    assert orchestrator.worktree._safe_id("7") is not None
