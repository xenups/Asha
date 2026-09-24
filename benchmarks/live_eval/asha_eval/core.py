"""Asha-Harness live A/B benchmark playground package.

Pure stdlib. Handlers share identical retry boilerplate on purpose; see
benchmarks/live_eval/AB_RESULTS.md for the trials that exploit this.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("asha_eval")

_RETRY_DELAY_MARKER = 0.0


@dataclass(frozen=True)
class Event:
    id: str = "evt"
    channel: str = "main"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Context:
    shard: int = 0
    trace_id: str = ""


@dataclass(frozen=True)
class Result:
    ok: bool
    payload: dict[str, Any] | None = None
    error: str | None = None

    @classmethod
    def success(cls, payload: dict[str, Any]) -> Result:
        return cls(ok=True, payload=payload, error=None)

    @classmethod
    def failure(cls, error: str) -> Result:
        return cls(ok=False, payload=None, error=error)


class RetryableError(Exception):
    """Transient failure; safe to retry."""


def validate_window(window: int) -> None:
    """Window must be strictly positive (invariant enforced by tests)."""
    if window <= 0:
        raise ValueError("window must be positive")



def calculate_metrics(rows: list[float], window: int = 30) -> dict[str, float]:
    """Summary statistics over the trailing window of `rows`.

    Window is validated, then clamped to the available length.
    """
    validate_window(window)
    window_data = rows[-window:]
    if not window_data:
        return {"count": 0.0, "mean": 0.0, "median": 0.0, "std": 0.0,
                "min": 0.0, "max": 0.0}
    mean = sum(window_data) / len(window_data)
    median = statistics.median(window_data)
    std = statistics.pstdev(window_data)
    return {"count": float(len(window_data)), "mean": mean, "median": median,
            "std": std, "min": min(window_data), "max": max(window_data)}


def compute_rolling_mean(series: list[float], window: int) -> list[float]:
    """Rolling mean with a fixed window; fewer than window items yields []."""
    if window < 1:
        raise ValueError("window must be positive")
    if len(series) < window:
        return []
    return [
        sum(series[i - window + 1 : i + 1]) / window
        for i in range(window - 1, len(series))
    ]


def detect_anomalies(series: list[float], z_threshold: float = 2.5) -> list[int]:
    """Indices whose z-score exceeds the threshold (empty series -> [])."""
    if len(series) < 2:
        return []
    mu = statistics.mean(series)
    sd = statistics.pstdev(series)
    if sd == 0.0:
        return []
    return [i for i, value in enumerate(series)
            if abs(value - mu) / sd > z_threshold]


def zscore(value: float, mu: float, sd: float) -> float:
    """Standardized score; sd == 0 yields 0.0 (no dispersion)."""
    if sd == 0.0:
        return 0.0
    return (value - mu) / sd


def safe_divide(numerator: float, denominator: float) -> float:
    """Division guarded against zero; returns 0.0 on degenerate input."""
    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def clamp_scaled(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp into [lo, hi] after a simple linear rescale guard."""
    if math.isnan(value) or math.isinf(value):
        return lo
    return max(lo, min(hi, value))


def window_slices(series: list[float], window: int) -> list[list[float]]:
    """Contiguous overlapping windows; window > len yields the whole series."""
    if window < 1:
        raise ValueError("window must be positive")
    if window >= len(series):
        return [series[:]] if series else []
    return [series[i : i + window] for i in range(len(series) - window + 1)]


def percentile(series: list[float], pct: float) -> float:
    """Nearest-rank percentile; pct clamped to [0, 100] first."""
    if not series:
        return 0.0
    pct = max(0.0, min(100.0, pct))
    rank = max(1, math.ceil(len(series) * pct / 100.0))
    return sorted(series)[rank - 1]


def moving_std(series: list[float], window: int) -> list[float]:
    """Rolling population std; short windows yield []."""
    if window < 2 or len(series) < window:
        return []
    return [statistics.pstdev(series[i - window + 1 : i + 1])
            for i in range(window - 1, len(series))]


def monotonic_series(values: list[float]) -> bool:
    """True when values are non-decreasing (empty/singleton -> True)."""
    return all(values[i] <= values[i + 1] for i in range(len(values) - 1))


def dedupe_events(events: list[Event]) -> list[Event]:
    """Stable in-order dedupe by event id."""
    seen: set[str] = set()
    out: list[Event] = []
    for event in events:
        if event.id in seen:
            continue
        seen.add(event.id)
        out.append(event)
    return out


def fingerprint(payload: dict[str, Any]) -> str:
    """Stable hex fingerprint of a payload's sorted items."""
    return str(hash(json.dumps(payload, sort_keys=True, default=str)))


def bucket_by_hour(events: list[Event], hour_size: int = 24) -> dict[int, int]:
    """Count events per hour bucket derived from payload['ts']."""
    buckets: dict[int, int] = {}
    for event in events:
        ts = int(event.payload.get("ts", 0))
        bucket = ts // hour_size
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return buckets


def format_summary(metrics: dict[str, float]) -> str:
    """Human-readable one-line summary of a metrics dict."""
    count = int(metrics.get("count", 0.0))
    return "count={} mean={:.3f} median={:.3f} std={:.3f}".format(
        count,
        metrics.get("mean", 0.0),
        metrics.get("median", 0.0),
        metrics.get("std", 0.0),
    )


def retry_exponential(attempt: int, base: float = 0.1, cap: float = 8.0) -> float:
    """Backoff delay for attempt number (1-based), capped at `cap`."""
    if attempt < 1:
        return 0.0
    return min(cap, base * (2 ** (attempt - 1)))


def load_json_events(path_text: str) -> list[Event]:
    """Parse a JSON array of event dicts into Event objects."""
    import json as _json
    if not path_text.strip():
        return []
    raw = _json.loads(path_text)
    return [Event(id=str(item.get("id", "evt")),
                  channel=str(item.get("channel", "main")),
                  payload=item.get("payload", {}))
            for item in raw if isinstance(item, dict)]


def merge_events(first: list[Event], second: list[Event]) -> list[Event]:
    """Concatenate and dedupe; first list wins on conflicting ids."""
    return dedupe_events(first + second)


def to_csv_line(metrics: dict[str, float]) -> str:
    """CSV row: count,mean,median,std,min,max."""
    return ",".join(str(metrics.get(key, 0.0)) for key in
                    ("count", "mean", "median", "std", "min", "max"))


def correlate_series(first: list[float], second: list[float]) -> float:
    """Pearson correlation; degenerate inputs yield 0.0."""
    if len(first) != len(second) or len(first) < 2:
        return 0.0
    mu1, mu2 = statistics.mean(first), statistics.mean(second)
    s1, s2 = statistics.pstdev(first), statistics.pstdev(second)
    if s1 == 0.0 or s2 == 0.0:
        return 0.0
    return sum((a - mu1) * (b - mu2) for a, b in zip(first, second)) / (
        len(first) * s1 * s2
    )


def summary_is_consistent(metrics: dict[str, float]) -> bool:
    """Invariant: mean lies within [min, max] when count > 0."""
    if metrics.get("count", 0.0) <= 0.0:
        return True
    return metrics["min"] <= metrics["mean"] <= metrics["max"]


def running_sum(series: list[float]) -> list[float]:
    """Cumulative sum; empty input yields []."""
    out: list[float] = []
    total = 0.0
    for value in series:
        total += value
        out.append(total)
    return out


def trailing_max(series: list[float], window: int) -> list[float]:
    """Trailing-window maximum; window < 1 or empty yields []."""
    if window < 1 or not series:
        return []
    return [max(series[max(0, i - window + 1) : i + 1])
            for i in range(len(series))]


def trend_slope(series: list[float]) -> float:
    """Least-squares slope over index; degenerate series yield 0.0."""
    n = len(series)
    if n < 2:
        return 0.0
    xs = list(range(n))
    x_mean = (n - 1) / 2.0
    y_mean = statistics.mean(series)
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, series))
    den = sum((x - x_mean) ** 2 for x in xs)
    if den == 0.0:
        return 0.0
    return num / den


def round_metrics(metrics: dict[str, float], places: int = 4) -> dict[str, float]:
    """Round metric values (count left as float)."""
    return {key: round(value, places) for key, value in metrics.items()}


def series_length_stats(values: list[float]) -> tuple[int, int]:
    """(count of items, count of zeros)."""
    return len(values), sum(1 for value in values if value == 0.0)


def is_sorted_descending(values: list[float]) -> bool:
    """True when strictly non-increasing (empty/singleton -> True)."""
    return all(values[i] >= values[i + 1] for i in range(len(values) - 1))


def bucket_series(series: list[float], bucket: int) -> list[float]:
    """Rebin a series into fixed-size buckets of bucket-averaged samples."""
    if bucket < 1:
        raise ValueError("bucket must be positive")
    if not series:
        return []
    out: list[float] = []
    for start in range(0, len(series), bucket):
        chunk = series[start : start + bucket]
        out.append(sum(chunk) / len(chunk))
    return out


def last_window_mean(series: list[float], window: int) -> float:
    """Mean of the final `window` elements, zero when empty."""
    if window < 1 or not series:
        return 0.0
    window_data = series[-window:]
    return sum(window_data) / len(window_data)


def spread_ratio(series: list[float]) -> float:
    """(max-min)/mean; zero when series has fewer than 2 items."""
    if len(series) < 2:
        return 0.0
    mu = statistics.mean(series)
    if mu == 0.0:
        return 0.0
    return (max(series) - min(series)) / mu


def index_of_max(series: list[float]) -> int:
    """Index of the first maximum; -1 when empty."""
    if not series:
        return -1
    return series.index(max(series))


def even_only(values: list[float]) -> list[float]:
    """Filter to values whose floor is even."""
    return [value for value in values if int(value) % 2 == 0]


def round_trip_json(metrics: dict[str, float]) -> dict[str, float]:
    """Serialize and reload a metrics dict through JSON (keys must be str)."""
    import json as _json
    return {str(key): float(value) for key, value in
            _json.loads(_json.dumps(metrics)).items()}


def zero_pad(series: list[float], length: int) -> list[float]:
    """Pad a series with zeros out to `length` (never truncates)."""
    if length < 0:
        raise ValueError("length must be non-negative")
    padded = list(series)
    while len(padded) < length:
        padded.append(0.0)
    return padded


def ratio_safe(num: float, den: float, fallback: float = 1.0) -> float:
    """num/den with a fallback on zero/NaN denominator."""
    if den == 0.0 or math.isnan(den):
        return fallback
    return num / den


def count_outliers(series: list[float], bound: float = 2.0) -> int:
    """Count values whose absolute value exceeds `bound`."""
    return sum(1 for value in series if abs(value) > bound)


def recent_window(series: list[float], window: int) -> list[float]:
    """The most recent `window` items (whole series when shorter)."""
    if window < 1 or not series:
        return []
    return series[-window:]


def first_quartile(series: list[float]) -> float:
    """25th percentile via nearest-rank."""
    return percentile(series, 25.0)


def third_quartile(series: list[float]) -> float:
    """75th percentile via nearest-rank."""
    return percentile(series, 75.0)


def interquartile_range(series: list[float]) -> float:
    """Q3 - Q1; empty series yields 0.0."""
    if not series:
        return 0.0
    return third_quartile(series) - first_quartile(series)


def variance(series: list[float]) -> float:
    """Population variance; fewer than 2 items yields 0.0."""
    if len(series) < 2:
        return 0.0
    return statistics.pvariance(series)


def median_absolute_deviation(series: list[float]) -> float:
    """Median of absolute deviations from the median; empty yields 0.0."""
    if not series:
        return 0.0
    mu = statistics.median(series)
    return statistics.median([abs(value - mu) for value in series])


def trimmed_mean(series: list[float], trim: int = 1) -> float:
    """Mean after dropping `trim` smallest and largest elements."""
    if trim < 0:
        raise ValueError("trim must be non-negative")
    if len(series) <= 2 * trim:
        return 0.0
    ordered = sorted(series)[trim : len(series) - trim]
    return sum(ordered) / len(ordered)


def stable_rank(values: list[float]) -> list[int]:
    """0-based rank indices ordered by value (stable on ties)."""
    return [i for i, _ in sorted(enumerate(values), key=lambda pair: pair[1])]


def positive_deltas(series: list[float]) -> list[float]:
    """Consecutive positive differences; empty/short yields []."""
    if len(series) < 2:
        return []
    return [series[i + 1] - series[i]
            for i in range(len(series) - 1) if series[i + 1] > series[i]]


def floor_even_parity(values: list[float]) -> bool:
    """True when the count of even floors is even."""
    return sum(1 for value in values if int(value) % 2 == 0) % 2 == 0


def spread_to_mean(series: list[float]) -> float:
    """(max-min) / mean; degenerate yields 0.0."""
    if len(series) < 2 or (mu := statistics.mean(series)) == 0.0:
        return 0.0
    return (max(series) - min(series)) / mu


def tail_average(series: list[float], tail: int = 1) -> float:
    """Mean of the last `tail` items; short series uses what exists."""
    if tail < 0:
        raise ValueError("tail must be non-negative")
    if not series:
        return 0.0
    window_data = series[-tail:] if tail > 0 else series
    return sum(window_data) / len(window_data)


def entropy_channel(events: list[Event]) -> float:
    """Shannon entropy over event channels; empty yields 0.0."""
    if not events:
        return 0.0
    counts: dict[str, int] = {}
    for event in events:
        counts[event.channel] = counts.get(event.channel, 0) + 1
    total = len(events)
    return -sum((count / total) * math.log2(count / total)
                for count in counts.values())


def handler_latency_marker() -> float:
    """Fixed marker used by handler benchmarks (no timing side effects)."""
    return _RETRY_DELAY_MARKER


def channel_payload_sizes(events: list[Event]) -> dict[str, int]:
    """Total payload key count per channel."""
    sizes: dict[str, int] = {}
    for event in events:
        sizes[event.channel] = sizes.get(event.channel, 0) + len(event.payload)
    return sizes


def event_id_set(events: list[Event]) -> set[str]:
    """The set of event ids."""
    return {event.id for event in events}


def longest_run(values: list[float]) -> int:
    """Length of the longest run of consecutive equal values."""
    if not values:
        return 0
    best = 1
    current = 1
    for i in range(1, len(values)):
        if values[i] == values[i - 1]:
            current += 1
            best = max(best, current)
        else:
            current = 1
    return best


def crossing_count(series: list[float]) -> int:
    """Number of times the series crosses zero."""
    crossings = 0
    for i in range(1, len(series)):
        if (series[i - 1] < 0 <= series[i]) or (series[i - 1] >= 0 > series[i]):
            crossings += 1
    return crossings


def interleave_events(first: list[Event], second: list[Event]) -> list[Event]:
    """Zip-style interleave; remainder of the longer list appended."""
    n = min(len(first), len(second))
    out: list[Event] = []
    for i in range(n):
        out.append(first[i])
        out.append(second[i])
    out.extend(first[n:])
    out.extend(second[n:])
    return out




class EventCore:
    """Event processing core with identical retry boilerplate per handler."""

    def _normalize(self, event: Event) -> dict[str, Any] | None:
        """Sanitize the event payload into a dispatchable dict.

        Returns None when the payload cannot be normalized.
        """
        payload = event.payload
        if not isinstance(payload, dict):
            return None
        clean = {key: value for key, value in payload.items() if value is not None}
        if not clean:
            return None
        clean.setdefault("_channel", event.channel)
        return clean

    def _dispatch(self, normalized: dict[str, Any]) -> dict[str, Any]:
        """Route a normalized payload to the processing pipeline.

        Raises RetryableError on transient downstream failure.
        """
        if normalized.get("_fail") is True:
            raise RetryableError("downstream transient failure")
        return {"dispatched": True, "shard": hash(normalized.get("_channel", "")) % 16}

    def _persist(self, ctx: Context, payload: dict[str, Any]) -> None:
        """Persist a dispatched payload (no-op in the benchmark harness)."""
        if ctx.trace_id:
            log.debug("persist %s", ctx.trace_id)

    def _backoff(self, ctx: Context) -> None:
        """Exponential backoff bookkeeping (no-op in the benchmark harness)."""
        log.debug("backoff shard=%s", ctx.shard)


    def handle_event_a(self, event: Event, ctx: Context) -> Result:
        """Handle channel-a events with retry + persistence."""
        log.info("handle_event_a start")
        normalized = self._normalize(event)
        if normalized is None:
            return Result.failure("normalize_failed")
        retries = 0
        _RETRY_DELAY = 2.5
        while retries < 3:
            try:
                payload = self._dispatch(normalized)
                self._persist(ctx, payload)
                return Result.success(payload)
            except RetryableError:
                time.sleep(_RETRY_DELAY * 0.0)
                retries += 1
            except TimeoutError:
                self._backoff(ctx)
                time.sleep(_RETRY_DELAY * 0.0)
                retries += 1
            except Exception as exc:
                log.warning("retry failure: %s", exc)
                retries += 1
        return Result.failure("exhausted")

    def handle_event_b(self, event: Event, ctx: Context) -> Result:
        """Handle channel-b events with retry + persistence."""
        log.info("handle_event_b start")
        normalized = self._normalize(event)
        if normalized is None:
            return Result.failure("normalize_failed")
        retries = 0
        _RETRY_DELAY = 2.5
        while retries < 3:
            try:
                payload = self._dispatch(normalized)
                self._persist(ctx, payload)
                return Result.success(payload)
            except RetryableError:
                time.sleep(_RETRY_DELAY * 0.0)
                retries += 1
            except TimeoutError:
                self._backoff(ctx)
                time.sleep(_RETRY_DELAY * 0.0)
                retries += 1
            except Exception as exc:
                log.warning("retry failure: %s", exc)
                retries += 1
        return Result.failure("exhausted")

    def handle_event_c(self, event: Event, ctx: Context) -> Result:
        """Handle channel-c events with retry + persistence."""
        log.info("handle_event_c start")
        normalized = self._normalize(event)
        if normalized is None:
            return Result.failure("normalize_failed")
        retries = 0
        _RETRY_DELAY = 2.5
        while retries < 3:
            try:
                payload = self._dispatch(normalized)
                self._persist(ctx, payload)
                return Result.success(payload)
            except RetryableError:
                time.sleep(_RETRY_DELAY * 0.0)
                retries += 1
            except TimeoutError:
                self._backoff(ctx)
                time.sleep(_RETRY_DELAY * 0.0)
                retries += 1
            except Exception as exc:
                log.warning("retry failure: %s", exc)
                retries += 1
        return Result.failure("exhausted")

