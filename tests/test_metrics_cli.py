"""cli_wiring checks; cross-worker binding activates only in union."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / ".hermes" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import metrics_cli


def test_parser_requires_command() -> None:
    with pytest.raises(SystemExit):
        metrics_cli.build_parser().parse_args([])


def test_record_unions_with_metrics() -> None:
    if (importlib.util.find_spec("metrics") is None
            or importlib.util.find_spec("report_format") is None):
        pytest.skip("cross-worker modules land in the union tree")
    assert metrics_cli.main(["record", "wiring-probe"]) == 0
