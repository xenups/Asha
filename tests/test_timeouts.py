"""Stage 1 contract: the centralized timeout policy and task deadline.

Pins the six historical defaults, rejects every invalid budget shape, and
fixes the precedence rule (explicit override > policy) plus the monotonic
deadline arithmetic. No call site consumes the policy yet -- this module is
deliberately inert until Stage 2 routes the constants through it.
"""

from __future__ import annotations

import dataclasses
import math
import time

import pytest

from asha import timeouts
from asha.timeouts import DEFAULT_TIMEOUTS, Deadline, TimeoutPolicy, resolve

HISTORICAL = {
    'worker': 600.0,
    'validation': 900.0,
    'check': 900.0,
    'baseline': 1800.0,
    'in_place': 600.0,
    'review': 900.0,
}


def test_defaults_match_every_historical_literal() -> None:
    """The policy is a routing layer, never a re-tuning."""
    for step, expected in HISTORICAL.items():
        assert resolve(step) == expected
        assert getattr(DEFAULT_TIMEOUTS, step) == expected


def test_steps_are_the_six_execution_budgets() -> None:
    assert set(timeouts.STEPS) == set(HISTORICAL)
    assert isinstance(DEFAULT_TIMEOUTS, TimeoutPolicy)


def test_explicit_override_wins_over_the_policy() -> None:
    assert resolve('check', 12.5) == 12.5
    assert resolve('worker', 1.0, policy=TimeoutPolicy(worker=2.0)) == 1.0


def test_policy_value_is_used_when_no_override_is_given() -> None:
    policy = TimeoutPolicy(check=42.0)
    assert resolve('check', None, policy=policy) == 42.0


@pytest.mark.parametrize('bad', [0, 0.0, -1, -0.5])
def test_zero_and_negative_budgets_are_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        TimeoutPolicy(worker=bad)
    with pytest.raises(ValueError):
        resolve('worker', bad)
    with pytest.raises(ValueError):
        Deadline(bad)


@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
def test_non_finite_budgets_are_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        TimeoutPolicy(validation=bad)
    with pytest.raises(ValueError):
        resolve('validation', bad)


@pytest.mark.parametrize('bad', [True, False])
def test_booleans_are_rejected_despite_being_ints(bad: bool) -> None:
    with pytest.raises(TypeError):
        TimeoutPolicy(check=bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolve('check', bad)


@pytest.mark.parametrize('bad', ['900', b'900', [], {}])
def test_non_numeric_budgets_are_rejected(bad: object) -> None:
    with pytest.raises(TypeError):
        resolve('check', bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Deadline(bad)  # type: ignore[arg-type]


def test_none_means_no_override_not_a_budget() -> None:
    assert resolve('check', None) == HISTORICAL['check']
    with pytest.raises(TypeError):
        Deadline(None)  # type: ignore[arg-type]


def test_unknown_policy_step_is_rejected_even_with_an_override() -> None:
    with pytest.raises(ValueError):
        resolve('worker_command')
    with pytest.raises(ValueError):
        resolve('worker_command', 5.0)
    with pytest.raises(ValueError):
        DEFAULT_TIMEOUTS.budget('nope')


def test_policy_is_frozen_and_values_are_floats() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        TimeoutPolicy().worker = 1.0  # type: ignore[misc]
    assert isinstance(TimeoutPolicy(worker=5).worker, float)


def test_deadline_counts_down_and_caps_operation_budgets() -> None:
    deadline = Deadline(2.0)
    assert deadline.seconds == 2.0
    assert 0.0 < deadline.remaining() <= 2.0
    assert deadline.expired() is False
    assert deadline.cap(30.0) <= 2.0          # task budget is binding
    assert deadline.cap(0.5) == 0.5           # operation budget is binding


def test_deadline_expires_without_resetting() -> None:
    deadline = Deadline(0.05)
    time.sleep(0.12)
    assert deadline.expired() is True
    assert deadline.remaining() == 0.0
    assert deadline.cap(30.0) == 0.0          # never extends itself


def test_check_budget_guards_a_caller_supplied_value() -> None:
    """A budget that arrives at runtime is validated once, where it is used."""
    assert timeouts.check_budget('check', 30) == 30.0
    assert isinstance(timeouts.check_budget('check', 30), float)
    with pytest.raises(ValueError):
        timeouts.check_budget('check', 0)
    with pytest.raises(ValueError):
        timeouts.check_budget('check', float('inf'))
    # step names are validated where a step is named, not here: check_budget
    # exists for a budget that arrives without one
    with pytest.raises(ValueError):
        resolve('no-such-step')


def test_no_deadline_means_no_cap() -> None:
    """Absent a deadline nothing is capped: the operation budget alone rules."""
    assert resolve('worker') == HISTORICAL['worker']
    with pytest.raises(TypeError):
        Deadline(None)  # type: ignore[arg-type]
