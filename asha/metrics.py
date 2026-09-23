"""In-process metric counters with atomic snapshot (Asha self-upgrade).

Delivered by worker `metrics_core`; consumed lazily by `metrics_cli` --
the cross-worker binding lives in the union tree, never in a single
worktree (PASS(A) + PASS(B) != PASS(A U B))."""
from __future__ import annotations

import threading
from collections import Counter

_LOCK = threading.Lock()
_COUNTER: Counter[str] = Counter()


def incr(name: str, value: int = 1) -> None:
    """Bump a named counter (thread-safe)."""
    with _LOCK:
        _COUNTER[name] += value


def snapshot() -> dict[str, int]:
    """Copy of current counts."""
    with _LOCK:
        return dict(_COUNTER)


def render(name: str, value: int) -> str:
    """Single-row rendering used by the CLI."""
    return f"{name}={value}"
