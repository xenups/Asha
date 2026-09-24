"""Phase 4.0 -- deterministic task & risk classification.

Pre-execution classifier: decides whether a task's dependency/state
surface is PROVEN disjoint from the other executions in its context.
Only PROVEN_DISJOINT ever earns fast-path eligibility; every ambiguity
fails closed to UNKNOWN, which maps to full governance:

    UNKNOWN != LOW_RISK   ->   UNKNOWN -> FULL_GOVERNANCE

Proof basis (the architecture's OWN granularity, not a new claim):
pairwise structural read/write analysis using the conflict module's
primitives -- the exact semantics dispatch safety calls
``proven_disjoint`` (conflict.py: file/module granularity only; unknown
sets are NEVER treated as empty; unprovable wildcard coverage counts as
overlap). Symbol-level signals (interfaces/shared symbols/AST) are NOT
extractable in this phase -- they are deliberately absent rather than
guessed; no signal here ever fabricates evidence.

Design rules:
  * deterministic, order-independent policy:
        proven shared surface  -> PROVEN_SHARED
        unresolved evidence    -> UNKNOWN
        proven disjoint        -> PROVEN_DISJOINT
  * proof REQUIRES an explicit context envelope: a missing/empty context
    is a missing signal (UNKNOWN), never a vacuous disjoint proof.
  * text/LLM intent in the task payload is NEVER a signal: enforcement
    reads only structured declarations (reads/writes/scope/deps).
  * wrong-shaped API input raises TypeError (programmer error);
    semantic uncertainty returns UNKNOWN (fail-closed, never raises).

Phase 4.0 computes eligibility ONLY -- the scheduler is untouched and
bypasses nothing until a later phase wires this in explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .conflict import _pair_conflict, scope_status
from .evidence import (
    EvidencePolicy,
    IndependenceClassification,
    resolve_evidence_policy,
)

_CLASS = IndependenceClassification


@dataclass(frozen=True)
class TaskClassification:
    classification: IndependenceClassification
    reason_code: str
    confidence_basis: str
    signals: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class GovernanceProfile:
    classification: IndependenceClassification
    fast_path_eligible: bool
    evidence_policy: EvidencePolicy
    isolation_required: bool
    replay_required: bool
    commitment_required: bool


def _unknown(reason: str, signals: tuple[tuple[str, str], ...]
             ) -> TaskClassification:
    return TaskClassification(
        classification=_CLASS.UNKNOWN,
        reason_code=reason,
        confidence_basis='insufficient_evidence',
        signals=tuple(sorted(signals)),
    )


def _as_path_list(task: dict[str, Any], key: str
                  ) -> tuple[list[str] | None, str]:
    """Known list -> entries; None -> explicit unknown; anything else is
    a programmer/API shape error (TypeError, not UNKNOWN)."""
    value = task.get(key)
    if value is None:
        return None, 'unknown'
    if not isinstance(value, list):
        raise TypeError(f'classifier: {key} must be list or None')
    if not all(isinstance(entry, str) for entry in value):
        raise TypeError(f'classifier: {key} entries must be str')
    return list(value), 'known'


def classify_task(task: dict[str, Any],
                  context: tuple[dict[str, Any], ...] = (),
                  ) -> TaskClassification:
    """Classify one task against the OTHER executions in ``context``.

    ``context`` must contain the other tasks (self, if present, is
    filtered by id). An empty context is a MISSING SIGNAL: claims about
    other executions cannot be proven against nothing.
    """
    if not isinstance(task, dict):
        raise TypeError('classifier: task must be dict')
    if not isinstance(context, tuple):
        raise TypeError('classifier: context must be tuple')

    signals: list[tuple[str, str]] = []

    reads, reads_state = _as_path_list(task, 'reads')
    writes, writes_state = _as_path_list(task, 'writes')
    signals.append(('reads', reads_state))
    signals.append(('writes', writes_state))

    # declared scope must be structurally valid (same primitive dispatch
    # refuses invalid declarations with)
    scope_ok, scope_why = scope_status(task)
    signals.append(('declared_scope', scope_why if scope_ok
                    else 'invalid'))

    others = tuple(other for other in context
                   if not isinstance(other, dict) or other.get('id')
                   != task.get('id'))
    signals.append(('context_size', str(len(others))))

    # dependency references must resolve inside the known context --
    # an unresolvable dependency is an unknown transitive surface
    deps = task.get('deps')
    if deps is None:
        signals.append(('deps', 'unknown'))
        return _unknown('unknown_dependency', tuple(signals))
    if not isinstance(deps, list) or not all(
            isinstance(dep, str) for dep in deps):
        raise TypeError('classifier: deps must be list of str')
    known_ids = {other.get('id') for other in others
                 if isinstance(other, dict)}
    unknown_deps = sorted(dep for dep in deps if dep not in known_ids)
    if unknown_deps:
        signals.append(('deps', 'unresolved:' + ','.join(unknown_deps)))
        return _unknown('unknown_dependency', tuple(signals))
    signals.append(('deps', 'known'))

    # missing/unknown surface signals block any proof
    if reads is None or writes is None:
        return _unknown('missing_signal_surface', tuple(signals))
    if not scope_ok:
        return _unknown('ambiguous_declared_scope', tuple(signals))
    if not others:
        return _unknown('missing_context', tuple(signals))
    signals.append(('explicitly_targeted_paths', 'present'))

    # pairwise structural analysis: ANY proven overlap wins first
    # (PROVEN_SHARED), evaluated over every other execution
    for other in sorted(others, key=lambda entry: str(entry.get('id'))):
        if not isinstance(other, dict):
            raise TypeError('classifier: context entries must be dict')
        other_reads = other.get('reads')
        other_writes = other.get('writes')
        if not isinstance(other_reads, list) or not isinstance(
                other_writes, list):
            # the OTHER side's surface is unknown: cannot prove either
            # direction -> UNKNOWN (never a disjoint claim)
            signals.append(('other_surface',
                            f'unknown:{other.get("id")}'))
            return _unknown('missing_signal_other_surface', tuple(signals))
        candidate = {'reads': reads, 'writes': writes}
        reason = _pair_conflict(candidate, other)
        if reason:
            signals.append((f'overlap:{other.get("id")}', reason))
            return TaskClassification(
                classification=_CLASS.PROVEN_SHARED,
                reason_code='shared_state_surface',
                confidence_basis='structural_overlap',
                signals=tuple(sorted(signals)),
            )

    # every surface known, scope valid, zero proven overlap across the
    # whole context -> structural disjointness at the conflict module's
    # stated granularity (file/module), same claim dispatch makes
    return TaskClassification(
        classification=_CLASS.PROVEN_DISJOINT,
        reason_code='no_proven_overlap_in_context',
        confidence_basis='pairwise_structural_disjointness',
        signals=tuple(sorted(signals)),
    )


def governance_profile(
        classification: IndependenceClassification | TaskClassification,
) -> GovernanceProfile:
    """Deterministic classification -> governance requirements mapping
    (Phase 4.0 contract section 9). ``resolve_evidence_policy`` keeps
    the evidence policy single-sourced with the Phase 3.0 contract."""
    if isinstance(classification, TaskClassification):
        resolved = classification.classification
    elif isinstance(classification, IndependenceClassification):
        resolved = classification
    else:
        raise TypeError('classifier: expected classification')
    eligible = resolved is _CLASS.PROVEN_DISJOINT
    return GovernanceProfile(
        classification=resolved,
        fast_path_eligible=eligible,
        evidence_policy=resolve_evidence_policy(resolved),
        isolation_required=not eligible,
        replay_required=not eligible,
        commitment_required=not eligible,
    )
