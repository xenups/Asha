"""Phase 4.2 -- deterministic, fail-closed runtime governance router.

Turns a Phase 4.0 ``GovernanceProfile`` into a runtime execution mode:

    PROVEN_DISJOINT + coherent eligible profile  -> FAST_PATH
    PROVEN_SHARED                                 -> FULL_GOVERNANCE
    UNKNOWN                                       -> FULL_GOVERNANCE
    missing / malformed / inconsistent / incomplete envelope
                                                  -> FULL_GOVERNANCE

Invariants (the hard gate of this phase):

  * ``UNKNOWN`` NEVER routes to FAST_PATH -- absence of evidence is
    not evidence of independence.
  * Classification and authorization are separate: the router reads
    the structured profile only; task text / LLM intent is never an
    input, and ``fast_path_eligible`` alone is not enough -- the whole
    profile must be internally coherent with the Phase 4.0 contract
    (eligible <=> PROVEN_DISJOINT <=> MINIMAL policy <=> no
    isolation/replay/commitment requirement).
  * The feature flag controls ROUTING only, never classification:
    flag off = every task follows the existing governance path.
  * ``NAIVE`` is never produced by routing -- it exists only as the
    experiment baseline (Path A of the three-way comparison).
  * deterministic: same inputs -> same decision; no clock, no
    entropy, no I/O, no LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .classifier import GovernanceProfile
from .evidence import EvidencePolicy, IndependenceClassification

_CLASS = IndependenceClassification


class RuntimeMode(Enum):
    NAIVE = 'naive'
    FULL_GOVERNANCE = 'full_governance'
    FAST_PATH = 'fast_path'


@dataclass(frozen=True)
class RouteDecision:
    mode: RuntimeMode
    reason_code: str
    classification: str | None     # None when no usable profile exists
    fast_path_enabled: bool


def _label(profile: object) -> str | None:
    if isinstance(profile, GovernanceProfile):
        classification = profile.classification
        if isinstance(classification, _CLASS):
            return classification.value
    return None


def _full(reason: str, profile: object,
          enabled: bool) -> RouteDecision:
    return RouteDecision(RuntimeMode.FULL_GOVERNANCE, reason,
                         _label(profile), enabled)


def _coherent(profile: GovernanceProfile) -> bool:
    """Whole-profile consistency with the Phase 4.0 contract."""
    if not isinstance(profile.fast_path_eligible, bool):
        return False
    classification = profile.classification
    if not isinstance(classification, _CLASS):
        return False
    if profile.fast_path_eligible:
        return (classification is _CLASS.PROVEN_DISJOINT
                and profile.evidence_policy is EvidencePolicy.MINIMAL
                and profile.isolation_required is False
                and profile.replay_required is False
                and profile.commitment_required is False)
    # ineligible profiles must carry the full-governance requirements
    return (classification is not _CLASS.PROVEN_DISJOINT
            and isinstance(profile.evidence_policy, EvidencePolicy)
            and isinstance(profile.isolation_required, bool)
            and isinstance(profile.replay_required, bool)
            and isinstance(profile.commitment_required, bool))


def route(profile: object, *, fast_path_enabled: bool,
          envelope_complete: bool = True) -> RouteDecision:
    """Deterministic fail-closed routing decision.

    ``envelope_complete=False`` records that the proof envelope is
    incomplete (e.g. an unresolved/dynamic dependency invalidated the
    proof) -- the profile is then ignored and the task runs under full
    governance, whatever it claims.
    """
    if not isinstance(fast_path_enabled, bool):
        raise TypeError('router: fast_path_enabled must be bool')
    if not isinstance(envelope_complete, bool):
        raise TypeError('router: envelope_complete must be bool')

    if not fast_path_enabled:
        # flag off: routing is FULL_GOVERNANCE for everything; the
        # classification label (if any) is reported, never acted on
        return _full('flag_disabled', profile, False)

    if not envelope_complete:
        return _full('incomplete_envelope', profile, True)

    if not isinstance(profile, GovernanceProfile):
        return _full('missing_or_malformed_profile', profile, True)

    if not _coherent(profile):
        # covers: unexpected classification value, bool/field tampering,
        # eligibility contradicted by any contract requirement
        return _full('inconsistent_profile', profile, True)

    classification = profile.classification
    if classification is _CLASS.PROVEN_SHARED:
        return _full('proven_shared', profile, True)
    if classification is _CLASS.UNKNOWN:
        return _full('unknown_never_fast_path', profile, True)
    if classification is _CLASS.PROVEN_DISJOINT and \
            profile.fast_path_eligible:
        return RouteDecision(RuntimeMode.FAST_PATH,
                             'proven_disjoint_coherent_profile',
                             classification.value, True)
    # unreachable with the current contract; kept fail-closed anyway
    return _full('unhandled_classification', profile, True)
