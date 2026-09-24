"""Phase: promote orchestrator to top-level asha/ (package tests).

importorskip keeps these tests honest under the engine's isolated-worker
verification: in a pre-migration worktree `asha` does not exist yet, so
they skip (never silently pass, never break collection); after the
governed integration they run against the real package.
"""
import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT),):
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


def test_entrypoint_scripts_resolve() -> None:
    """Registered console-script targets resolve without running the CLI."""
    assert importlib.util.find_spec("asha.__main__") is not None
    assert importlib.util.find_spec("asha.mcp_server") is not None
