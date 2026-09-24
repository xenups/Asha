"""Strict pytest suite for the asha_eval package.

Asserts behavior, type-hint annotations, and invariants. Any mutation that
breaks an exact metric value, the window-validation boundary, the handler
dispatch contract, or the annotated signatures must fail here.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from asha_eval import core
from asha_eval.core import Context, Event, EventCore, calculate_metrics, validate_window

# --- behavior: exact metric values (Trial C target) ----------------------

def test_metrics_mean_exact() -> None:
    metrics = calculate_metrics([1.0, 2.0, 3.0])
    assert metrics["mean"] == 2.0
    assert metrics["median"] == 2.0


def test_metrics_std_exact() -> None:
    metrics = calculate_metrics([1.0, 1.0, 1.0])
    assert metrics["std"] == 0.0
    metrics2 = calculate_metrics([0.0, 2.0])
    assert metrics2["mean"] == 1.0


def test_metrics_window_positive_boundary() -> None:
    """window=0 must raise ValueError (subtle '<=' vs '<' regression target)."""
    with pytest.raises(ValueError):
        calculate_metrics([1.0, 2.0], window=0)
    with pytest.raises(ValueError):
        calculate_metrics([1.0, 2.0], window=-3)


def test_metrics_window_clamped() -> None:
    metrics = calculate_metrics(list(range(10)), window=100)
    assert metrics["count"] == 10.0
    assert metrics["min"] == 0.0
    assert metrics["max"] == 9.0


def test_metrics_empty_input() -> None:
    metrics = calculate_metrics([])
    assert metrics["count"] == 0.0
    assert metrics["mean"] == 0.0


# --- type hints / signatures (Trial B target) ----------------------------

def test_calculate_metrics_annotated() -> None:
    sig = inspect.signature(calculate_metrics)
    assert sig.parameters["rows"].annotation is not inspect.Parameter.empty
    assert sig.parameters["window"].default == 30


def test_handlers_annotate_result() -> None:
    core_module = EventCore
    for name in ("handle_event_a", "handle_event_b", "handle_event_c"):
        method = getattr(core_module, name)
        # `from __future__ import annotations` makes these strings.
        assert method.__annotations__["return"] == "Result"
        assert "event" in method.__annotations__


def test_validate_window_annotated() -> None:
    assert validate_window.__annotations__["window"] == "int"


# --- behavior: handler dispatch contract (Trial A) ------------------------

def _spy_core(monkeypatch: pytest.MonkeyPatch) -> EventCore:
    calls: list[str] = []

    def fake_dispatch(self, normalized: dict) -> dict:
        calls.append(str(normalized.get("_channel")))
        return {"dispatched": True, "shard": 0}

    monkeypatch.setattr(EventCore, "_dispatch", fake_dispatch)
    core_instance = EventCore()
    return core_instance


def test_handler_a_dispatches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _spy_core(monkeypatch)
    result = instance.handle_event_a(Event(id="1", channel="a", payload={"k": 1}),
                                     Context(shard=1, trace_id="t1"))
    assert result.ok is True
    assert result.payload == {"dispatched": True, "shard": 0}


def test_handler_b_dispatches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _spy_core(monkeypatch)
    result = instance.handle_event_b(Event(id="2", channel="b", payload={"k": 2}),
                                     Context(shard=2, trace_id="t2"))
    assert result.ok is True
    assert result.payload == {"dispatched": True, "shard": 0}


def test_handler_c_dispatches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _spy_core(monkeypatch)
    result = instance.handle_event_c(Event(id="3", channel="c", payload={"k": 3}),
                                     Context(shard=3, trace_id="t3"))
    assert result.ok is True
    assert result.payload == {"dispatched": True, "shard": 0}


def test_handler_exhaustion_returns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = EventCore()
    result = instance.handle_event_a(Event(id="4", channel="a", payload={}),
                                     Context(shard=0, trace_id=""))
    assert result.ok is False
    assert result.error == "normalize_failed"


# --- invariants -----------------------------------------------------------

def test_mean_within_min_max_invariant() -> None:
    rows = [float(value) for value in range(1, 51)]
    for window in (1, 5, 30, 200):
        assert core.summary_is_consistent(calculate_metrics(rows, window=window))


def test_validate_window_rejects_nonpositive_direct() -> None:
    with pytest.raises(ValueError):
        validate_window(0)
    with pytest.raises(ValueError):
        validate_window(-1)
    validate_window(1)
