"""Semantic reviewer contract (Phase 2: P2.1).

Single responsibility: represent the structured findings a semantic
Python reviewer emits, and the severity policy that decides whether
findings block ship authorization. No execution, no I/O -- pure schema
validation, mirroring the design of asha.gate_states.

Schema (machine form of the Review Finding Format in
docs/reviewer/SENIOR_PYTHON_REVIEWER_SPEC.md):

    severity         BLOCKER | HIGH | MEDIUM | LOW   (strict enum)
    file_path        repository-relative path of the finding
    line_range       line span as text, e.g. '42' or '10-25'
    problem          the concrete problem
    why_it_matters   production/security/correctness consequence
    failure_scenario realistic scenario that causes the problem
    recommended_fix  safest fix consistent with the existing codebase

Every field is required and must be a non-empty string; an empty or
missing field, an unknown severity, or a non-dict entry raises
GateContractError (fail closed -- a malformed finding must never become
a clean authorization).

Policy:

    SEVERITY_BLOCKS_SHIP = {BLOCKER, HIGH}

Under policy_version 'phase2' (see gate_states.evaluate), findings
whose severity is in this set deny authorization; MEDIUM/LOW findings
record COMPLETED_WITH_FINDINGS without blocking. Zero findings record
COMPLETED_CLEAN. Policy 'phase1' keeps the historical rule: any
findings -> COMPLETED_WITH_FINDINGS -> denied.

The reviewer artifact that CARRIES findings is bound to the audited
snapshot by gate_states (schema 'semantic-review/v1', snapshot_id
matching the evidence). This module never constructs authorizations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from asha.gate_states import GateContractError

# Artifact format carried alongside evidence; part of the authorization
# binding, so a schema change can never silently reuse old artifacts.
REVIEW_SCHEMA = 'semantic-review/v1'


class Severity(str, Enum):
    """Strict severity ladder; no defaults, no unknown values."""

    BLOCKER = 'BLOCKER'
    HIGH = 'HIGH'
    MEDIUM = 'MEDIUM'
    LOW = 'LOW'


SEVERITY_VALUES: frozenset[str] = frozenset(
    severity.value for severity in Severity)

SEVERITY_BLOCKS_SHIP: frozenset[Severity] = frozenset(
    {Severity.BLOCKER, Severity.HIGH})

# String forms for JSON-shaped findings (artifact payloads never carry
# enum instances across serialization).
BLOCKING_SEVERITY_STRINGS: frozenset[str] = frozenset(
    severity.value for severity in SEVERITY_BLOCKS_SHIP)

FINDING_FIELDS: tuple[str, ...] = (
    'severity', 'file_path', 'line_range',
    'problem', 'why_it_matters', 'failure_scenario', 'recommended_fix',
)


@dataclass(frozen=True)
class Finding:
    """One reviewer finding, validated at construction (fail closed).

    Severity is stored as its string value; ``severity_level`` exposes
    the typed enum. Any empty field, unknown severity, or non-string
    field raises GateContractError so a malformed finding can never be
    normalized into a valid one downstream.
    """

    severity: str
    file_path: str
    line_range: str
    problem: str
    why_it_matters: str
    failure_scenario: str
    recommended_fix: str

    def __post_init__(self) -> None:
        if not isinstance(self.severity, str) or self.severity not in SEVERITY_VALUES:
            raise GateContractError(
                f'finding severity {self.severity!r} is not one of '
                f'{sorted(SEVERITY_VALUES)}')
        for field_name in FINDING_FIELDS[1:]:
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise GateContractError(
                    f'finding field {field_name!r} must be a non-empty '
                    f'string, got {value!r}')

    @property
    def severity_level(self) -> Severity:
        """Typed severity for policy decisions."""
        return Severity(self.severity)

    @property
    def blocks_ship(self) -> bool:
        """True when this finding denies authorization under phase2."""
        return self.severity in BLOCKING_SEVERITY_STRINGS


def validate_finding(entry: object, *, index: int | None = None) -> dict:
    """Validate one raw finding entry; return it unchanged on success.

    Raises GateContractError for a non-dict entry, a missing/empty
    required field, or an unknown severity. ``index`` only decorates the
    failure message when the entry comes from a list.
    """
    where = '' if index is None else f' (#{index})'
    if not isinstance(entry, dict):
        raise GateContractError(
            f'finding{where} must be an object, got '
            f'{type(entry).__name__}')
    missing = [key for key in FINDING_FIELDS if key not in entry]
    if missing:
        raise GateContractError(
            f'finding{where} missing required fields: '
            + ', '.join(sorted(missing)))
    # Finding() performs the strict type/emptiness/enum checks; the
    # constructed object is discarded -- the serialized dict stays the
    # evidence shape.
    Finding(**{key: entry[key] for key in FINDING_FIELDS})
    return entry


def validate_findings(entries: object) -> list[dict]:
    """Validate a whole findings collection; fail closed on the first bad entry."""
    if not isinstance(entries, (list, tuple)):
        raise GateContractError(
            f'findings must be a list, got {type(entries).__name__}')
    return [validate_finding(entry, index=index)
            for index, entry in enumerate(entries)]


def validate_artifact(artifact: object, *, snapshot_id: str) -> dict:
    """Validate the reviewer artifact's identity fields and findings.

    The artifact must be a dict carrying schema/identity plus a findings
    list; its snapshot_id MUST equal the audited snapshot the decision
    binds to. Anything else raises GateContractError (INVALID_EVIDENCE
    at the gate). The severity partition itself is decided by
    gate_states.evaluate() -- never here.
    """
    if not isinstance(artifact, dict):
        raise GateContractError(
            f'semantic review evidence must be an object, got '
            f'{type(artifact).__name__}')
    missing = [key for key in ('schema', 'snapshot_id', 'policy_version')
               if key not in artifact]
    if missing:
        raise GateContractError(
            'semantic review evidence missing identity fields: '
            + ', '.join(sorted(missing)))
    if artifact['schema'] != REVIEW_SCHEMA:
        raise GateContractError(
            f'unknown semantic review schema {artifact["schema"]!r}; '
            f'expected {REVIEW_SCHEMA!r}')
    artifact_snapshot = artifact['snapshot_id']
    if not isinstance(artifact_snapshot, str) or not artifact_snapshot.strip():
        raise GateContractError(
            'semantic review evidence requires a non-empty snapshot_id '
            '(fail closed)')
    if artifact_snapshot != snapshot_id:
        raise GateContractError(
            'semantic review evidence is bound to a different snapshot: '
            f'reviewed {artifact_snapshot[:12]}..., '
            f'audited {snapshot_id[:12]}...')
    validate_findings(artifact.get('findings') or [])
    return artifact
