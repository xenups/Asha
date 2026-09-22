"""Downstream consumer of asha_eval.core — 5 calculate_metrics call sites."""
from __future__ import annotations


import asha_eval.core as core
from asha_eval.core import calculate_metrics


def hourly_metrics(samples: list[float], window: int = 30) -> dict[str, float]:
    """Hourly rollup via the trailing-window metrics function."""
    return calculate_metrics(samples, window=window)


def daily_summary(samples: list[float]) -> dict[str, float]:
    """Daily summary using the default window."""
    metrics = calculate_metrics(samples)
    return {"daily_mean": metrics["mean"], "daily_count": metrics["count"]}


def trend(rows: list[float]) -> float:
    """Mean of the trailing window used by the trend view."""
    return core.calculate_metrics(rows, window=12)["mean"]


def alert_if_spike(rows: list[float], threshold: float = 3.0) -> bool:
    """Alert when the window mean exceeds a multiple of the global mean."""
    window_mean = core.calculate_metrics(rows)["mean"]
    if window_mean == 0.0:
        return False
    global_mean = sum(rows) / max(1, len(rows))
    return window_mean > threshold * global_mean


def export_row(rows: list[float], label: str) -> str:
    """One CSV line: label + metric summary."""
    metrics = core.calculate_metrics(rows)
    return f"{label},{metrics['count']},{metrics['mean']:.3f}"
