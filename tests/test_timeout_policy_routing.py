"""Stage 2 contract: one authoritative source per execution budget.

The six constants keep their historical names and values (callers import
them), but the numbers now come from ``asha.timeouts.DEFAULT_TIMEOUTS`` so a
budget can never be re-tuned in two places at once.
"""

from __future__ import annotations

from asha import check_runner, mcp_server, timeouts, types, validation
from asha.governance import ship_gate

HISTORICAL = {
    'worker': 600,
    'validation': 900,
    'check': 900,
    'baseline': 1800,
    'in_place': 600,
    'review': 900,
}


def test_compatibility_aliases_keep_their_historical_names_and_values() -> None:
    assert types.WORKER_TIMEOUT_S == HISTORICAL['worker']
    assert validation.VALIDATION_TIMEOUT_S == HISTORICAL['validation']
    assert check_runner.CHECK_TIMEOUT_S == HISTORICAL['check']
    assert check_runner.BASELINE_TIMEOUT_S == HISTORICAL['baseline']
    assert mcp_server._IN_PLACE_TIMEOUT_S == HISTORICAL['in_place']
    assert ship_gate.REVIEW_TIMEOUT_S == HISTORICAL['review']


def test_every_alias_is_the_policy_value() -> None:
    policy = timeouts.DEFAULT_TIMEOUTS
    assert type(policy).__name__ == 'TimeoutPolicy'
    assert types.WORKER_TIMEOUT_S == policy.worker
    assert validation.VALIDATION_TIMEOUT_S == policy.validation
    assert check_runner.CHECK_TIMEOUT_S == policy.check
    assert check_runner.BASELINE_TIMEOUT_S == policy.baseline
    assert mcp_server._IN_PLACE_TIMEOUT_S == policy.in_place
    assert ship_gate.REVIEW_TIMEOUT_S == policy.review


def test_no_execution_budget_is_still_a_bare_literal() -> None:
    """A literal at a call site would silently shadow the policy."""
    assert timeouts.resolve('worker') == HISTORICAL['worker']
    assert timeouts.resolve('validation') == HISTORICAL['validation']
    assert timeouts.resolve('check') == HISTORICAL['check']
    assert timeouts.resolve('baseline') == HISTORICAL['baseline']
    assert timeouts.resolve('in_place') == HISTORICAL['in_place']
    assert timeouts.resolve('review') == HISTORICAL['review']


def test_transport_and_scheduling_budgets_stay_outside_the_policy() -> None:
    """live_reviewer's LLM transport budget and the DAG wave poll are not
    execution budgets: Phase 4 and the scheduler own them."""
    from asha.governance import dag, live_reviewer

    assert live_reviewer.DEFAULT_TIMEOUT_S == 120
    assert dag._WAVE_OVERLAP_S == 0.5
    assert set(timeouts.STEPS) == set(HISTORICAL)
