"""summary_formatter unit checks (worker-authored)."""
from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / ".hermes" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import report_format


def test_format_sorted_rows() -> None:
    assert report_format.format_table({"b": 2, "a": 1}) == "a=1\nb=2"


def test_format_empty_table() -> None:
    assert report_format.format_table({}) == "(no metrics)"
