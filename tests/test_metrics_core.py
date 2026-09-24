"""metrics_core unit checks (worker-authored; green in its own tree)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asha import metrics


def test_incr_snapshot_roundtrip() -> None:
    before = metrics.snapshot().get("unit-rt", 0)
    metrics.incr("unit-rt")
    metrics.incr("unit-rt", 2)
    assert metrics.snapshot()["unit-rt"] == before + 3


def test_render_single_row() -> None:
    assert metrics.render("builds", 7) == "builds=7"
