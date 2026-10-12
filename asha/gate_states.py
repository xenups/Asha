"""Typed gate-state contract for ship authorization (Phase 1: Stage 2/3).

Single responsibility: represent the outcome of an authorization attempt as
an explicit, typed state, and validate audit evidence structurally against
that contract. No execution, no I/O, no git -- pure validation so it can be
reasoned about and tested in isolation.

Why explicit states instead of a boolean:

    The pre-Phase-1 predicate was `all(c["status"] in ("passed",
    "skipped") for c in checks)`. On an EMPTY check inventory that is
    `all([]) == True`, so a run that executed nothing authorized shipping
    (proven by probe: gate_ship(no_execute=True) sealed
    authorized_to_ship=True with checks == []). A boolean also cannot
    distinguish a clean run from a crashed one, a missing check from a
    skipped one, or a stale snapshot from a fresh one -- all of which the
    operator must act on differently.

Vocabulary (no defaults, no implicit success):

    COMPLETED_CLEAN          every required check completed successfully
                             AND the evidence passed validation.
    COMPLETED_WITH_FINDINGS  every required check completed, but at least
                             one reported findings. Phase 1 defines NO
                             permissive severity policy, so this state is
                             NOT authorization on its own: the gate denies
                             unless a policy explicitly allows it.
    INVALID_EVIDENCE         evidence is malformed, incomplete, bound to a
                             different snapshot/policy, or internally
                             inconsistent.
    EXECUTION_FAILED         a check crashed, timed out, or did not
                             complete.
    STALE_REVIEW             the authorization target no longer matches the
                             audited snapshot (TOCTOU).

`authorized_to_ship` is DERIVED from this state -- never accepted as an
input. STATE_ALLOWS_SHIP names the only state that grants authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GateState(Enum):
    """Explicit, total outcome vocabulary for an authorization attempt."""

    COMPLETED_CLEAN = 'COMPLETED_CLEAN'
    COMPLETED_WITH_FINDINGS = 'COMPLETED_WITH_FINDINGS'
    INVALID_EVIDENCE = 'INVALID_EVIDENCE'
    EXECUTION_FAILED = 'EXECUTION_FAILED'
    STALE_REVIEW = 'STALE_REVIEW'


# The ONLY state that grants authorization. COMPLETED_WITH_FINDINGS is
# deliberately absent: Phase 1 has no severity policy, so findings never
# authorize by themselves (spec: "do not invent a permissive severity
# policy").
STATE_ALLOWS_SHIP: frozenset[GateState] = frozenset({GateState.COMPLETED_CLEAN})

# Per-check execution status vocabulary (mirrors check_runner exactly).
CHECK_PASSED = 'passed'
CHECK_FAILED = 'failed'
CHECK_SKIPPED = 'skipped'
CHECK_STATUSES: frozenset[str] = frozenset(
    {CHECK_PASSED, CHECK_FAILED, CHECK_SKIPPED})

# Exit-code contract, established from the ACTUAL runner (check_runner.py):
#
#   run()        passed  -> 0            failed -> nonzero, or -1 on
#                                        OSError/TimeoutExpired
#                skipped -> 0            (entry starts as
#                                        {'status': 'passed',
#                                         'exit_code': 0} and the empty-
#                                        argv branch only flips status)
#   scoped runner           passed  -> 0            failed -> nonzero, or None on
#                                        timeout (entry starts with
#                                        exit_code None and the timeout
#                                        branch does not set it)
#                skipped -> 0            (empty-argv branch sets status to
#                                        'skipped', exit_code stays at its
#                                        initial 0)
#
# Only scoped-runner timeout-failures carry exit_code None. So:
#
#   * a `passed` entry with nonzero or None exit code cannot be produced
#     by either runner -> internally inconsistent evidence;
#   * a `failed` entry must carry a code (nonzero) or None (scoped
#     timeout); 0 is contradictory;
#   * a PROVEN skip is `skipped` WITH exit code 0 -- the shape
#     check_runner.run() produces for a check it did not launch (the
#     entry initializes exit_code 0 and the empty-argv branch flips
#     status to 'skipped'). `skipped` with None or 5 is the SCOPED
#     runner's vocabulary (empty-argv None, pytest exit-5), and the
#     scoped runner does not feed this authorization contract; a
#     hand-built `skipped` with None is indistinguishable from a
#     timeout-failure, so it fails closed as INVALID_EVIDENCE.
#
# The runner proves INAPPLICABILITY structurally (empty argv emitted by
# the execution path, never by evidence). The note string ('no applicable
# target', 'empty_target_set_proven', 'no_tests_collected') is a human
# label for that same structural event and carries NO authority of its
# own -- this is the F1 hardening: a note can be authored by hand. The
# authoritative signal at THIS boundary is exit_code == 0 on a skipped
# entry, which only check_runner.run()'s empty-argv path produces.


class GateContractError(Exception):
    """Evidence violated the typed contract (fail-closed)."""


@dataclass(frozen=True)
class CheckResult:
    """One required check's execution outcome.

    ``note`` is a free-text human label from the runner (e.g. 'no applicable
    target'). It is INFORMATIONAL ONLY and carries no authority: legitimate
    skip eligibility is derived from the execution-shape invariants below,
    never from the note string. Anything that string-matches a note could be
    authored by hand.

    Execution-shape invariants (see the exit-code contract above):

    * ``passed``  requires ``exit_code == 0``;
    * ``failed``  requires a nonzero ``exit_code`` or ``None`` (a scoped
      timeout records no code); ``failed`` with ``0`` is contradictory;
    * ``skipped`` requires ``exit_code == 0`` -- the shape
      ``check_runner.run()`` produces for a check it did not launch
      (empty argv). ``None`` and ``5`` are the SCOPED runner's shapes,
      which does not feed this contract; a ``skipped`` check with any
      other code -- or a string or bool -- is rejected here and by
      ``validate_evidence`` (fail closed, never normalized into a
      valid skip).

    An entry that violates its own status is internally inconsistent
    evidence and is rejected here, at construction, so it can never be
    interpreted as a clean outcome downstream.
    """

    name: str
    status: str
    exit_code: int | None = None
    note: str = ''

    def __post_init__(self) -> None:
        if not self.name:
            raise GateContractError('check entry requires a name')
        if self.status not in CHECK_STATUSES:
            raise GateContractError(
                f'unknown check status {self.status!r} for {self.name!r}; '
                f'expected one of {sorted(CHECK_STATUSES)}')
        if self.status == CHECK_PASSED and self.exit_code != 0:
            raise GateContractError(
                f'check {self.name!r} is PASSED but its exit code is '
                f'{self.exit_code!r}; a passed check must exit 0 '
                '(inconsistent execution evidence)')
        if self.status == CHECK_FAILED and self.exit_code == 0:
            raise GateContractError(
                f'check {self.name!r} is FAILED but its exit code is 0 '
                '(inconsistent execution evidence)')
        if self.status == CHECK_SKIPPED and self.exit_code != 0:
            raise GateContractError(
                f'check {self.name!r} is SKIPPED but records exit code '
                f'{self.exit_code!r}; at the ship authorization boundary a '
                'proven skip carries 0 (the code check_runner.run() writes '
                'before deciding not to launch); None/5 belong to the '
                'scoped runner, which does NOT feed this contract, so '
                'anything else is a check whose execution never completed '
                '(inconsistent execution evidence)')
        if isinstance(self.exit_code, bool):
            raise GateContractError(
                f'check {self.name!r} exit code must be an integer or '
                f'None, got {type(self.exit_code).__name__} (bool is '
                'not a valid exit code)')

    @property
    def is_proven_skip(self) -> bool:
        """A skip proven at the authorization boundary.

        The proof is the execution shape check_runner.run() actually
        produces for a check it did not launch: ``skipped`` with exit
        code 0 (the code written before the empty-argv branch flipped
        the status). Any other exit-code shape -- None (scoped-runner
        timeout/empty-argv), 5 (scoped pytest exit-5), nonzero, a
        string, or a bool -- is either a different runner's vocabulary
        or a hand-built entry, and is NOT proven here. The note string
        is informational and carries no authority.
        """
        return self.status == CHECK_SKIPPED and self.exit_code == 0


@dataclass(frozen=True)
class GateEvidence:
    """The evidence contract an authorization decision is derived from.

    Every field is required: a missing field is INVALID_EVIDENCE, never a
    default. ``required_checks`` is the inventory the policy demanded;
    ``checks`` is what actually ran -- an incomplete inventory is
    inconsistent and must never be read as success.
    """

    snapshot_id: str
    evidence_schema: int
    policy_version: str
    required_checks: tuple[str, ...]
    checks: tuple[CheckResult, ...]
    findings: tuple[dict, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            raise GateContractError('snapshot_id is required')
        if not isinstance(self.evidence_schema, int) or self.evidence_schema < 1:
            raise GateContractError('evidence_schema must be a positive int')
        if not self.policy_version:
            raise GateContractError('policy_version is required')
        if not self.required_checks:
            raise GateContractError(
                'required_checks inventory is empty -- an empty inventory '
                'cannot authorize anything (fail closed)')
        if len(set(self.required_checks)) != len(self.required_checks):
            raise GateContractError('required_checks contains duplicates')
        names = [check.name for check in self.checks]
        if len(set(names)) != len(names):
            raise GateContractError('checks contains duplicate entries')

    def by_name(self) -> dict[str, CheckResult]:
        return {check.name: check for check in self.checks}


def validate_evidence(evidence: object) -> GateEvidence:
    """Structurally validate an evidence payload; fail closed.

    Accepts a GateEvidence (returned as-is) or a mapping. Anything else --
    None, a list, a malformed dict, an unknown check status, a missing
    required field -- raises GateContractError. A payload that validates is
    NOT yet authorization: evaluate() decides that.
    """
    if isinstance(evidence, GateEvidence):
        return evidence
    if not isinstance(evidence, dict):
        raise GateContractError(
            f'evidence must be an object, got {type(evidence).__name__}')
    missing = [key for key in ('snapshot_id', 'evidence_schema',
                               'policy_version', 'required_checks', 'checks')
               if key not in evidence]
    if missing:
        raise GateContractError(
            'evidence missing required fields: ' + ', '.join(sorted(missing)))
    raw_checks = evidence['checks']
    if not isinstance(raw_checks, list):
        raise GateContractError('checks must be a list')
    checks: list[CheckResult] = []
    for entry in raw_checks:
        if not isinstance(entry, dict):
            raise GateContractError('each check entry must be an object')
        if 'name' not in entry or 'status' not in entry:
            raise GateContractError(
                'check entry requires name and status: ' + repr(entry))
        exit_code = entry.get('exit_code')
        note = entry.get('note')
        if exit_code is not None and (not isinstance(exit_code, int)
                                      or isinstance(exit_code, bool)):
            # Reject malformed exit-code types instead of silently
            # normalizing them: a string from a tampered payload must
            # not become a valid skip by coercion.
            raise GateContractError(
                f'check {entry.get("name")!r} exit code must be an '
                f'integer or None, got {type(exit_code).__name__}')
        checks.append(CheckResult(
            name=str(entry['name']),
            status=str(entry['status']),
            exit_code=exit_code,
            note=str(note) if isinstance(note, str) else ''))
    required = evidence['required_checks']
    if not isinstance(required, (list, tuple)):
        raise GateContractError('required_checks must be a list')

    findings = evidence.get('findings') or []
    if not isinstance(findings, (list, tuple)):
        raise GateContractError('findings must be a list')
    for index, item in enumerate(findings):
        if not isinstance(item, dict):
            # Never silently drop an evidence field: a finding the
            # validator cannot represent must DENY, not vanish into a
            # clean authorization.
            raise GateContractError(
                f'finding #{index} must be an object, got '
                f'{type(item).__name__}; malformed findings are rejected '
                '(fail closed)')
    return GateEvidence(
        snapshot_id=str(evidence['snapshot_id']),
        evidence_schema=evidence['evidence_schema'],
        policy_version=str(evidence['policy_version']),
        required_checks=tuple(str(name) for name in required),
        checks=tuple(checks),
        findings=tuple(dict(item) for item in findings),
    )


@dataclass(frozen=True)
class GateDecision:
    """The typed outcome plus the facts that produced it."""

    state: GateState
    reasons: tuple[str, ...] = ()

    @property
    def authorized_to_ship(self) -> bool:
        """Derived, never accepted as input."""
        return self.state in STATE_ALLOWS_SHIP

    def as_dict(self) -> dict:
        return {
            'gate_state': self.state.value,
            'authorized_to_ship': self.authorized_to_ship,
            'reasons': list(self.reasons),
        }


def _missing_and_unexpected(evidence: GateEvidence) -> tuple[list[str], list[str]]:
    required = set(evidence.required_checks)
    observed = set(evidence.by_name())
    missing = sorted(required - observed)
    unexpected = sorted(observed - required)
    return missing, unexpected


def evaluate(evidence: GateEvidence,
             authorized_snapshot_id: str) -> GateDecision:
    """Derive the gate state from validated evidence.

    ``authorized_snapshot_id`` is MANDATORY and carries no default: the
    identity of the snapshot being authorized is part of the decision, so a
    caller that cannot supply it gets a fail-closed verdict rather than a
    silently skipped freshness check. (Before Phase 1 hardening this
    defaulted to ``None`` and disabled the STALE_REVIEW branch entirely --
    the freshness guarantee then rested only on every caller remembering the
    keyword.)

    Ordered, fail-closed checks -- the FIRST failure wins, so the reason
    names the actual blocker:

      1. missing/blank authorized identity   -> INVALID_EVIDENCE (fail closed)
      2. snapshot binding mismatch           -> STALE_REVIEW
      3. check inventory incomplete          -> INVALID_EVIDENCE
      4. any check did not complete          -> INVALID_EVIDENCE / EXECUTION_FAILED
      5. any check reported findings         -> COMPLETED_WITH_FINDINGS
      6. otherwise                           -> COMPLETED_CLEAN (authorizes)
    """
    if not isinstance(authorized_snapshot_id, str) or not authorized_snapshot_id.strip():
        return GateDecision(GateState.INVALID_EVIDENCE, (
            ('authorized snapshot identity is required and must be a '
             'non-empty string; freshness cannot be established without it '
             '(fail closed)'),))
    if authorized_snapshot_id != evidence.snapshot_id:
        return GateDecision(GateState.STALE_REVIEW, (
            ('authorization target does not match the audited snapshot: '
            f'audited {evidence.snapshot_id[:12]}..., '
            f'target {authorized_snapshot_id[:12]}...'),))
    # A per-check SKIP is a permitted outcome (the runner proved an empty
    # target set), but an inventory in which NOTHING executed is not: that
    # is the same fail-open class as the historical all([]) bug, reached
    # through the skip path. No policy permits an all-skipped audit to
    # authorize shipping, so it denies.
    if all(check.is_proven_skip for check in evidence.checks):
        return GateDecision(GateState.INVALID_EVIDENCE, (
            ('no check executed: every required check was skipped, so nothing '
             'was verified (fail closed)'),))

    missing, unexpected = _missing_and_unexpected(evidence)
    if missing:
        return GateDecision(GateState.INVALID_EVIDENCE, (
            'required checks did not run: ' + ', '.join(missing),))
    if unexpected:
        return GateDecision(GateState.INVALID_EVIDENCE, (
            'executed checks absent from the policy inventory: '
            + ', '.join(unexpected),))

    by_name = evidence.by_name()
    # A required check that did not run denies. check_runner.run() proves a
    # skip with exit_code==0 (empty argv); __post_init__ already rejects any
    # other skipped shape, so this branch is unreachable through the
    # constructor. It stays as a second, independent line of defence at the
    # decision boundary: it reads exit_code directly instead of the
    # is_proven_skip property, so a future drift in that property cannot
    # silently re-open the forged-skip authorization bypass (F1).
    not_run = sorted(name for name in evidence.required_checks
                     if by_name[name].status == CHECK_SKIPPED
                     and by_name[name].exit_code != 0)
    if not_run:
        return GateDecision(GateState.INVALID_EVIDENCE, (
            'required checks did not run (skipped without a proven empty '
            'target set): ' + ', '.join(not_run),))
    failed = sorted(name for name in evidence.required_checks
                    if by_name[name].status == CHECK_FAILED)
    if failed:
        return GateDecision(GateState.EXECUTION_FAILED, (
            'checks failed: ' + ', '.join(failed),))

    if evidence.findings:
        return GateDecision(GateState.COMPLETED_WITH_FINDINGS, (
            (f'{len(evidence.findings)} finding(s) reported; Phase 1 has no '
            'policy that authorizes shipping with findings'),))
    return GateDecision(GateState.COMPLETED_CLEAN, ())
