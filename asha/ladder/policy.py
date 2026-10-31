"""Review policy: which stages are mandatory and what independence counts.

The policy is data with a content hash, so a persisted run names exactly
the rules it was judged by. A run never reads its policy from the
artifact under review, and a reviewer cannot influence it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from asha.ladder.models import (
    BLOCKING_SEVERITIES,
    Independence,
    Severity,
    StageType,
)

POLICY_SCHEMA = 1


@dataclass(frozen=True)
class ReviewPolicy:
    """Mandatory stages, independence floor and blocking severities."""

    policy_id: str
    required_stages: tuple[StageType, ...]
    required_independence: Independence
    blocking_severities: frozenset[Severity] = BLOCKING_SEVERITIES

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ValueError('policy_id must be non-empty')
        if not self.required_stages:
            raise ValueError('a policy must require at least one stage')
        if not isinstance(self.required_independence, Independence):
            raise TypeError('required_independence must be an Independence')

    def as_dict(self) -> dict[str, object]:
        return {
            'policy_schema': POLICY_SCHEMA,
            'policy_id': self.policy_id,
            'required_stages': [stage.value for stage in self.required_stages],
            'required_independence': self.required_independence.value,
            'blocking_severities': sorted(
                severity.value for severity in self.blocking_severities),
        }

    def content_hash(self) -> str:
        """SHA-256 over the canonical policy document (stable ordering)."""
        body = json.dumps(self.as_dict(), sort_keys=True,
                          separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha256(body.encode('utf-8')).hexdigest()


def default_policy() -> ReviewPolicy:
    """Both review passes, semantic validation and the Ship Gate, required."""
    return ReviewPolicy(
        policy_id='ladder/default',
        required_stages=(
            StageType.CODE_REVIEW,
            StageType.SENIOR_REVIEW,
            StageType.SEMANTIC_REVIEW,
            StageType.SHIP_GATE,
        ),
        required_independence=Independence.SAME_PROVIDER_ISOLATED_CONTEXT,
    )


def distinct_provider_policy() -> ReviewPolicy:
    """Stricter: the two review passes must come from different providers."""
    return ReviewPolicy(
        policy_id='ladder/distinct-provider',
        required_stages=(
            StageType.CODE_REVIEW,
            StageType.SENIOR_REVIEW,
            StageType.SEMANTIC_REVIEW,
            StageType.SHIP_GATE,
        ),
        required_independence=Independence.DISTINCT_PROVIDER,
    )


_POLICIES = {
    'default': default_policy,
    'ladder/default': default_policy,
    'distinct-provider': distinct_provider_policy,
    'ladder/distinct-provider': distinct_provider_policy,
}


def resolve_policy(name: str | None) -> ReviewPolicy:
    """Look up an approved policy by id; unknown ids fail closed."""
    if name is None or not name.strip():
        return default_policy()
    factory = _POLICIES.get(name.strip().lower())
    if factory is None:
        raise ValueError(
            f'unknown review policy {name!r}; approved: '
            + ', '.join(sorted(set(_POLICIES) - {'ladder/default',
                                                 'ladder/distinct-provider'})))
    return factory()
