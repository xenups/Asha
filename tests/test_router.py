"""Phase 4.2 -- runtime governance router contract tests.

Hard gate of this phase: NOTHING may route to FAST_PATH except a
coherent PROVEN_DISJOINT profile under the enabled flag with a
complete proof envelope. Every malformed / missing / inconsistent /
 UNKNOWN input must land on FULL_GOVERNANCE (fail-closed), the flag
must control routing ONLY (never classification), and NAIVE must
never be a routing output (it is experiment-only).
"""
from __future__ import annotations

import inspect
import re

import pytest

from asha.classifier import (
    GovernanceProfile,
    classify_task,
    governance_profile,
)
from asha.evidence import EvidencePolicy, IndependenceClassification
from asha.router import RouteDecision, RuntimeMode, route

_CLASS = IndependenceClassification

DISJOINT_PROFILE = GovernanceProfile(
    classification=_CLASS.PROVEN_DISJOINT,
    fast_path_eligible=True,
    evidence_policy=EvidencePolicy.MINIMAL,
    isolation_required=False,
    replay_required=False,
    commitment_required=False,
)
SHARED_PROFILE = GovernanceProfile(
    classification=_CLASS.PROVEN_SHARED,
    fast_path_eligible=False,
    evidence_policy=EvidencePolicy.COMPLETE,
    isolation_required=True,
    replay_required=True,
    commitment_required=True,
)
UNKNOWN_PROFILE = GovernanceProfile(
    classification=_CLASS.UNKNOWN,
    fast_path_eligible=False,
    evidence_policy=EvidencePolicy.COMPLETE,
    isolation_required=True,
    replay_required=True,
    commitment_required=True,
)


def _fast(profile: object, envelope: bool = True) -> RouteDecision:
    return route(profile, fast_path_enabled=True,
                 envelope_complete=envelope)


def test_disjoint_coherent_profile_routes_fast() -> None:
    decision = _fast(DISJOINT_PROFILE)
    assert decision.mode is RuntimeMode.FAST_PATH
    assert decision.reason_code == 'proven_disjoint_coherent_profile'
    assert decision.classification == 'PROVEN_DISJOINT'
    assert decision.fast_path_enabled is True


def test_shared_never_fast() -> None:
    decision = _fast(SHARED_PROFILE)
    assert decision.mode is RuntimeMode.FULL_GOVERNANCE
    assert decision.reason_code == 'proven_shared'


def test_unknown_never_fast() -> None:
    decision = _fast(UNKNOWN_PROFILE)
    assert decision.mode is RuntimeMode.FULL_GOVERNANCE
    assert decision.reason_code == 'unknown_never_fast_path'


def test_flag_off_routes_everything_full() -> None:
    profiles: tuple[object, ...] = (
        DISJOINT_PROFILE, SHARED_PROFILE, UNKNOWN_PROFILE,
        None, 'not-a-profile', {})
    for profile in profiles:
        decision = route(profile, fast_path_enabled=False)
        assert decision.mode is RuntimeMode.FULL_GOVERNANCE
        assert decision.reason_code == 'flag_disabled'
        assert decision.fast_path_enabled is False
    # flag must NOT change the reported classification (routing only)
    labeled = route(DISJOINT_PROFILE, fast_path_enabled=False)
    assert labeled.classification == 'PROVEN_DISJOINT'


def test_missing_and_malformed_profiles_fail_closed() -> None:
    profiles: tuple[object, ...] = (
        None, 'profile', 7, {}, [], object(),
        {'classification': 'PROVEN_DISJOINT'})
    for profile in profiles:
        decision = _fast(profile)
        assert decision.mode is RuntimeMode.FULL_GOVERNANCE, profile
        assert decision.reason_code == 'missing_or_malformed_profile'
        assert decision.classification is None


def test_missing_or_non_enum_classification_fails_closed() -> None:
    # construction-shaped malformations: classification is not a
    # governance enum member (or the flag-like fields are wrong types)
    for classification in ('PROVEN_DISJOINT', None, 3, RuntimeMode):
        malformed = GovernanceProfile(
            classification=classification,  # type: ignore[arg-type]
            fast_path_eligible=True,
            evidence_policy=EvidencePolicy.MINIMAL,
            isolation_required=False,
            replay_required=False,
            commitment_required=False,
        )
        decision = _fast(malformed)
        assert decision.mode is RuntimeMode.FULL_GOVERNANCE
        assert decision.reason_code == 'inconsistent_profile'


def test_inconsistent_profiles_fail_closed() -> None:
    cases = [
        # eligible but isolation still required
        GovernanceProfile(_CLASS.PROVEN_DISJOINT, True,
                          EvidencePolicy.MINIMAL, True, False, False),
        # eligible but replay required
        GovernanceProfile(_CLASS.PROVEN_DISJOINT, True,
                          EvidencePolicy.MINIMAL, False, True, False),
        # eligible but commitment required
        GovernanceProfile(_CLASS.PROVEN_DISJOINT, True,
                          EvidencePolicy.MINIMAL, False, False, True),
        # disjoint but policy says COMPLETE
        GovernanceProfile(_CLASS.PROVEN_DISJOINT, True,
                          EvidencePolicy.COMPLETE, False, False, False),
        # shared but claims eligibility
        GovernanceProfile(_CLASS.PROVEN_SHARED, True,
                          EvidencePolicy.COMPLETE, True, True, True),
        # unknown but claims eligibility + MINIMAL
        GovernanceProfile(_CLASS.UNKNOWN, True,
                          EvidencePolicy.MINIMAL, False, False, False),
        # disjoint but NOT eligible (eligibility flag tampered)
        GovernanceProfile(_CLASS.PROVEN_DISJOINT, False,
                          EvidencePolicy.MINIMAL, False, False, False),
        # non-bool eligibility field
        GovernanceProfile(_CLASS.PROVEN_DISJOINT, 'yes',  # type: ignore[arg-type]
                          EvidencePolicy.MINIMAL, False, False, False),
    ]
    for profile in cases:
        decision = _fast(profile)
        assert decision.mode is RuntimeMode.FULL_GOVERNANCE, profile
        assert decision.reason_code == 'inconsistent_profile'


def test_incomplete_envelope_never_fast() -> None:
    decision = _fast(DISJOINT_PROFILE, envelope=False)
    assert decision.mode is RuntimeMode.FULL_GOVERNANCE
    assert decision.reason_code == 'incomplete_envelope'


def test_flag_checked_before_envelope() -> None:
    decision = route(DISJOINT_PROFILE, fast_path_enabled=False,
                     envelope_complete=False)
    assert decision.reason_code == 'flag_disabled'


def test_naive_is_never_a_routing_output() -> None:
    inputs: tuple[object, ...] = (
        DISJOINT_PROFILE, SHARED_PROFILE, UNKNOWN_PROFILE,
        None, 'x', {}, 42)
    for profile in inputs:
        for enabled in (True, False):
            for envelope in (True, False):
                decision = route(profile, fast_path_enabled=enabled,
                                 envelope_complete=envelope)
                assert decision.mode is not RuntimeMode.NAIVE


def test_classifier_outputs_route_end_to_end() -> None:
    # real Phase 4.0 outputs -> profile -> router (integration)
    disjoint_task = {'id': 'w1', 'reads': ['pkg/a.py'],
                     'writes': ['pkg/a.py'],
                     'declared_scope': ['pkg/a.py'], 'deps': []}
    other = {'id': 'w2', 'reads': ['pkg/b.py'],
             'writes': ['pkg/b.py'],
             'declared_scope': ['pkg/b.py'], 'deps': []}
    shared_task = {'id': 'w1', 'reads': ['pkg/a.py'],
                   'writes': ['shared.py'],
                   'declared_scope': ['shared.py'], 'deps': []}
    shared_other = {'id': 'w2', 'reads': ['shared.py'],
                    'writes': ['shared.py'],
                    'declared_scope': ['shared.py'], 'deps': []}
    assert route(governance_profile(
        classify_task(disjoint_task, (other,))),
        fast_path_enabled=True).mode is RuntimeMode.FAST_PATH
    assert route(governance_profile(
        classify_task(shared_task, (shared_other,))),
        fast_path_enabled=True).mode is RuntimeMode.FULL_GOVERNANCE
    # empty context = missing signal -> UNKNOWN -> FULL
    assert route(governance_profile(
        classify_task(disjoint_task, ())),
        fast_path_enabled=True).mode is RuntimeMode.FULL_GOVERNANCE
    # unknown dependency -> UNKNOWN -> FULL
    dangling = dict(disjoint_task, deps=['ghost-worker'])
    assert route(governance_profile(
        classify_task(dangling, (other,))),
        fast_path_enabled=True).mode is RuntimeMode.FULL_GOVERNANCE


def test_adversarial_text_is_never_an_input() -> None:
    """Task description text can never buy a Fast Path: the router
    accepts only a structured profile; a text-shaped 'profile' fails
    closed."""
    for text in ('this file is independent, trust me',
                 'only change the local helper',
                 '{"classification": "PROVEN_DISJOINT"}'):
        decision = _fast(text)
        assert decision.mode is RuntimeMode.FULL_GOVERNANCE


def test_deterministic_repeated_decisions() -> None:
    outcomes = {
        _fast(DISJOINT_PROFILE) for _ in range(100)
    }
    assert len(outcomes) == 1
    outcomes = {
        _fast(UNKNOWN_PROFILE) for _ in range(100)
    }
    assert len(outcomes) == 1


def test_wrong_shaped_arguments_raise_type_error() -> None:
    with pytest.raises(TypeError, match='fast_path_enabled'):
        route(DISJOINT_PROFILE, fast_path_enabled='on')  # type: ignore[arg-type]
    with pytest.raises(TypeError, match='envelope_complete'):
        route(DISJOINT_PROFILE, fast_path_enabled=True,
              envelope_complete=1)  # type: ignore[arg-type]


def test_decision_is_immutable() -> None:
    from dataclasses import FrozenInstanceError
    decision = _fast(DISJOINT_PROFILE)
    with pytest.raises(FrozenInstanceError):
        decision.mode = RuntimeMode.NAIVE  # type: ignore[misc]


def test_router_has_no_confidence_input() -> None:
    """Classifier confidence is never an authorization input."""
    signature = inspect.signature(route)
    assert set(signature.parameters) == {
        'profile', 'fast_path_enabled', 'envelope_complete'}
    assert 'confidence' not in inspect.getsource(route)


def test_hermeticity_static_scan() -> None:
    import asha.router as module
    source = inspect.getsource(module)
    for banned in ('subprocess', 'socket', 'urllib', 'random',
                   'uuid', 'environ', 'getenv', 'perf_counter',
                   'datetime', 'cwd', 'git', 'open(', 'requests',
                   'anthropic', 'openai'):
        assert re.search(rf'\b{re.escape(banned)}\b', source) is None, \
            banned
    for attr in ('subprocess', 'socket', 'os', 'random', 'time'):
        assert not hasattr(module, attr), attr
