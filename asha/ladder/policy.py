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

POLICY_SCHEMA = 2

#: Every policy requires all three host review passes and the Ship Gate.
REQUIRED_STAGES: tuple[StageType, ...] = (
    StageType.CODE_REVIEW,
    StageType.SENIOR_REVIEW,
    StageType.SEMANTIC_REVIEW,
    StageType.SHIP_GATE,
)


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
    """The practical default: three host passes, no independence claim.

    Asha cannot verify how many models the host used, so the default
    requires the work (three role-specific passes, validated findings,
    the Ship Gate) and makes no claim about reviewer independence.
    """
    return ReviewPolicy(
        policy_id='ladder/host-multipass',
        required_stages=REQUIRED_STAGES,
        required_independence=Independence.UNKNOWN,
    )


def isolated_host_policy() -> ReviewPolicy:
    """Stricter: the host must attest an isolated context for each pass.

    Still an attestation, not verified proof: it gates what the record
    claims, it does not create independence Asha cannot see.
    """
    return ReviewPolicy(
        policy_id='ladder/host-isolated',
        required_stages=REQUIRED_STAGES,
        required_independence=Independence.HOST_ATTESTED_ISOLATED_CONTEXT,
    )


_POLICIES = {
    'host-multipass': default_policy,
    'ladder/host-multipass': default_policy,
    'default': default_policy,
    'host-isolated': isolated_host_policy,
    'ladder/host-isolated': isolated_host_policy,
}


def resolve_policy(name: str | None) -> ReviewPolicy:
    """Look up an approved policy by id; unknown ids fail closed."""
    if name is None or not name.strip():
        return default_policy()
    factory = _POLICIES.get(name.strip().lower())
    if factory is None:
        raise ValueError(
            f'unknown review policy {name!r}; approved: '
            + ', '.join(sorted({'host-multipass', 'host-isolated'})))
    return factory()
