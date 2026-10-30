"""Centralized execution timeout policy and opt-in task deadlines.

The six execution budgets Asha applies to a child process -- worker command,
validation check, matrix check, baseline pass, in-place command, semantic
review -- have ONE authoritative source here, plus an optional task-level
``Deadline``.

Stdlib only, and no environment variables: a budget changes only through an
explicit argument at the call site (``resolve``) or a replacement policy
object, and a whole task is bounded only by passing one ``Deadline``.

Invariants:

* the defaults ARE the historical literals, so routing a call site through the
  policy changes nothing until a caller overrides it;
* an invalid budget fails at construction -- zero, negative, NaN, infinity,
  ``bool`` (a subclass of ``int`` in Python), a string, or an unknown step
  name. A silently zero budget would kill every child immediately and an
  infinite one would never kill at all;
* a ``Deadline`` reads ``time.monotonic()`` only and is never reset, so a
  later stage cannot extend the task budget by starting after an earlier one.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Final, Literal

Step = Literal['worker', 'validation', 'check', 'baseline', 'in_place',
               'review']

STEPS: Final[tuple[Step, ...]] = ('worker', 'validation', 'check',
                                 'baseline', 'in_place', 'review')


def _budget(label: str, value: object) -> float:
    """A budget is a finite, strictly positive real number of seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f'{label} timeout must be a real number, '
                        f'got {type(value).__name__}')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'{label} timeout must be finite, got {number!r}')
    if number <= 0.0:
        raise ValueError(f'{label} timeout must be > 0, got {number!r}')
    return number


@dataclass(frozen=True)
class TimeoutPolicy:
    """The six execution budgets, validated when the policy is built."""

    worker: float = 600.0
    validation: float = 900.0
    check: float = 900.0
    baseline: float = 1800.0
    in_place: float = 600.0
    review: float = 900.0

    def __post_init__(self) -> None:
        object.__setattr__(self, 'worker', _budget('worker', self.worker))
        object.__setattr__(self, 'validation',
                           _budget('validation', self.validation))
        object.__setattr__(self, 'check', _budget('check', self.check))
        object.__setattr__(self, 'baseline',
                           _budget('baseline', self.baseline))
        object.__setattr__(self, 'in_place',
                           _budget('in_place', self.in_place))
        object.__setattr__(self, 'review', _budget('review', self.review))

    def budget(self, step: str) -> float:
        """The policy value for one step; unknown names are refused."""
        if step == 'worker':
            return self.worker
        if step == 'validation':
            return self.validation
        if step == 'check':
            return self.check
        if step == 'baseline':
            return self.baseline
        if step == 'in_place':
            return self.in_place
        if step == 'review':
            return self.review
        raise ValueError(f'unknown timeout step {step!r} '
                         f'(known: {", ".join(STEPS)})')


DEFAULT_TIMEOUTS = TimeoutPolicy()


def resolve(step: str, explicit: float | None = None,
            policy: TimeoutPolicy = DEFAULT_TIMEOUTS) -> float:
    """Effective budget for one operation.

    Precedence is deterministic: an explicit override when one was supplied,
    otherwise the centralized policy. The step name is validated either way,
    so a typo cannot silently fall back to a different budget.
    """
    policy.budget(step)  # raises on an unknown step name
    if explicit is None:
        return policy.budget(step)
    return _budget(step, explicit)


class Deadline:
    """One task budget, measured on the monotonic clock only.

    ``cap`` returns the budget a single operation may use: the smaller of its
    own budget and what is left of the task. ``0.0`` means the task budget is
    exhausted and the operation must not launch at all.
    """

    __slots__ = ('_end', '_seconds')

    def __init__(self, seconds: float) -> None:
        self._seconds = _budget('deadline', seconds)
        self._end = time.monotonic() + self._seconds

    @property
    def seconds(self) -> float:
        return self._seconds

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def cap(self, budget: float) -> float:
        """min(operation budget, remaining task budget); 0.0 = do not launch."""
        return max(0.0, min(_budget('budget', budget), self.remaining()))
