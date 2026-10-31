"""Live LLM-backed semantic-review executor (Phase 3).

Single responsibility: turn the audited repository state into a
``semantic-review/v1`` artifact by calling an OpenAI-compatible
``chat/completions`` endpoint, and resolve that executor for ship_gate.
``semantic_review`` stays pure (schema only, no I/O).

Fail-closed contract (Phase 1/2 invariants untouched):

* transport / configuration / parse failures raise ``ReviewerError``,
  which ``ship_gate._run_reviewer`` maps to a FAILED
  ``semantic-review`` check entry -> EXECUTION_FAILED (deny);
* ``resolve_live_reviewer()`` returns ``None`` unless the live flag AND
  credentials are present -> ship_gate's phase2 'missing artifact'
  INVALID_EVIDENCE path stays in force. There is NO mock/stub fallback
  on any path -- a mock can only enter through the explicit ``reviewer``
  parameter (tests);
* the outgoing prompt carries ``snapshot_id`` and the incoming artifact
  must echo it: a missing echo fails as malformed output
  (EXECUTION_FAILED), a WRONG echo is returned as-is so the gate's
  ``validate_artifact`` rejects it (INVALID_EVIDENCE).

Configuration (env, following the repo's ASHA_* convention):

    ASHA_SEMANTIC_REVIEW        'live' enables auto-resolution (default: off)
    ASHA_LLM_API_KEY            required when live (never defaulted)
    ASHA_LLM_BASE_URL           default https://api.deepseek.com
    ASHA_LLM_MODEL              default deepseek-chat
    ASHA_LLM_TIMEOUT_S          per-request timeout, default 120;
                                clamped to the caller's timeout_s budget
    ASHA_LLM_MAX_TOKENS         spending bound per review, default 4096
    ASHA_LLM_TEMPERATURE        default 0.0
    ASHA_REVIEW_CONTEXT_BUDGET  file-context chars, default 60000

One review = ONE bounded request (single-shot spending ceiling).
ponytail: the transport is OpenAI-compatible (DeepSeek, Gemini's
OpenAI endpoint); an Anthropic Messages-API transport and 429 backoff
retry get added when actually needed -- today a rate limit fails closed
as EXECUTION_FAILED, never a silent pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from asha import audit_snapshot, scope_resolver
from asha.gate_states import GateContractError
from asha.governance import semantic_review

# Executor contract mirrors ship_gate.ReviewerFn (not imported: ship_gate
# imports THIS module at its top level).
ReviewerFn = Callable[[Path, str, int], dict]

DEFAULT_BASE_URL = 'https://api.deepseek.com'
DEFAULT_MODEL = 'deepseek-chat'
DEFAULT_TIMEOUT_S = 120
DEFAULT_MAX_TOKENS = 4096
DEFAULT_CONTEXT_BUDGET = 60_000

# Machine contract distilled from docs/reviewer/SENIOR_PYTHON_REVIEWER_SPEC.md
# (severity ladder + Review Finding Format); embedded so the prompt does
# not depend on docs/ being present in the reviewed repository.
PROMPT_SYSTEM = """You are the Senior Python Reviewer running as an \
automated gate in the Asha harness. Review ONLY the repository context \
in the user message, against the policy in
docs/reviewer/SENIOR_PYTHON_REVIEWER_SPEC.md (repository copy): \
correctness, consistency with existing code, security, concurrency, \
reliability, scalability, maintainability.

Output EXACTLY ONE JSON OBJECT, no prose, no markdown fences:

{
  "snapshot_id": "<echo the snapshot_id from the user message VERBATIM>",
  "findings": [
    {
      "severity": "BLOCKER|HIGH|MEDIUM|LOW",
      "file_path": "repo-relative/path.py",
      "line_range": "42" or "10-25",
      "problem": "the concrete problem",
      "why_it_matters": "production/security/correctness consequence",
      "failure_scenario": "realistic scenario that causes it",
      "recommended_fix": "safest fix consistent with the codebase"
    }
  ]
}

Rules:
* "findings" MUST be an array; use [] when nothing meaningful is wrong.
* Every finding needs ALL SEVEN fields, each a non-empty string, and a \
severity EXACTLY one of BLOCKER, HIGH, MEDIUM, LOW.
* BLOCKER/HIGH blocks shipping; MEDIUM/LOW does not. Do not inflate \
severity; every finding needs a concrete failure scenario.
* The snapshot_id MUST be echoed verbatim. Never omit it."""


class ReviewerError(Exception):
    """Live review could not produce valid evidence (fail closed)."""


@dataclass(frozen=True)
class LiveReviewerConfig:
    """Resolved, validated live-review settings (one bounded request)."""

    api_key: str
    base_url: str
    model: str
    timeout_s: int
    max_tokens: int
    temperature: float
    context_budget: int


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ReviewerError(
            f'{name} must be an integer, got {raw!r}') from exc


def _config_from_env(timeout_s: int) -> LiveReviewerConfig:
    """Build the request config; missing key/malformed values fail closed."""
    key = os.environ.get('ASHA_LLM_API_KEY', '').strip()
    if not key:
        raise ReviewerError(
            'live semantic review requested but ASHA_LLM_API_KEY is not '
            'set (fail closed)')
    requested = _env_int('ASHA_LLM_TIMEOUT_S', DEFAULT_TIMEOUT_S)
    budget = max(1, min(requested, timeout_s))
    return LiveReviewerConfig(
        api_key=key,
        base_url=os.environ.get(
            'ASHA_LLM_BASE_URL', DEFAULT_BASE_URL).strip().rstrip('/'),
        model=os.environ.get('ASHA_LLM_MODEL', DEFAULT_MODEL).strip()
        or DEFAULT_MODEL,
        timeout_s=budget,
        max_tokens=max(1, _env_int('ASHA_LLM_MAX_TOKENS',
                                   DEFAULT_MAX_TOKENS)),
        temperature=float(os.environ.get('ASHA_LLM_TEMPERATURE', '0')
                          or 0),
        context_budget=max(1, _env_int('ASHA_REVIEW_CONTEXT_BUDGET',
                                       DEFAULT_CONTEXT_BUDGET)),
    )


def resolve_live_reviewer() -> ReviewerFn | None:
    """Auto-resolve the live executor from env; None unless BOTH the flag
    and credentials are present.

    None (flag off, or live requested without a key) is NOT a fallback:
    ship_gate then has no artifact and phase2 denies INVALID_EVIDENCE.
    A key without the flag stays inert (opt-in is explicit).
    """
    if os.environ.get('ASHA_SEMANTIC_REVIEW', '').strip().lower() != 'live':
        return None
    if not os.environ.get('ASHA_LLM_API_KEY', '').strip():
        return None
    return run_live_semantic_reviewer


def _endpoint(base_url: str) -> str:
    if base_url.endswith('/chat/completions'):
        return base_url
    return base_url + '/chat/completions'


def _http_post(url: str, payload: dict, config: LiveReviewerConfig
               ) -> dict:
    """Default transport (stdlib urllib; the ONLY network code in asha/).

    Raises whatever urllib raises; run_live_semantic_reviewer maps
    HTTPError/URLError/timeouts to ReviewerError.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {config.api_key}',
        },
        method='POST')
    with urllib.request.urlopen(request, timeout=config.timeout_s) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _review_context(root: Path, budget: int) -> str:
    """Snapshot-verified context: affected files, digest-checked against
    the audit manifest, budget-capped (spending + prompt-size bound).

    A file whose content does not hash to its manifest digest means the
    context is NOT the audited snapshot -> ReviewerError (fail closed),
    never a review of unverifiable bytes.
    """
    manifest = audit_snapshot.build_manifest(root)
    index = manifest.index()
    resolved = scope_resolver.resolve(root)
    parts: list[str] = []
    used = 0
    for rel in sorted(str(name) for name in resolved['affected_files']):
        entry = index.get(rel)
        if entry is None or not entry.digest:
            # not in the audited snapshot (or a deletion) -- nothing to
            # hash, nothing to review here.
            continue
        path = root / rel
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ReviewerError(
                f'cannot read audited context file {rel}: {exc}') from exc
        if hashlib.sha256(data).hexdigest() != entry.digest:
            raise ReviewerError(
                f'context file {rel} does not match the audited snapshot '
                'digest (fail closed)')
        if used >= budget:
            parts.append(f'### FILE: {rel}\n[truncated: context budget '
                         f'{budget} chars exhausted]')
            continue
        text = data.decode('utf-8', errors='replace')
        room = budget - used
        clipped = text if len(text) <= room else (
            text[:room] + f'\n... [truncated at {room} chars]')
        parts.append(f'### FILE: {rel}\n{clipped}')
        used += len(clipped)
    if not parts:
        return ('(no changed files in this snapshot; review for anything '
                'wrong you can still see)')
    return '\n\n'.join(parts)


def _message_content(raw: dict) -> str:
    """Extract choices[0].message.content from an OpenAI-style response."""
    try:
        content = raw['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as exc:
        raise ReviewerError(
            f'unexpected LLM response shape: {type(exc).__name__}'
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise ReviewerError('LLM response carried no message content')
    return content


def _parse_json(content: str) -> dict:
    """Parse strict JSON; tolerate ONE layer of markdown fencing (models
    fence despite instructions), reject everything else."""
    text = content.strip()
    if text.startswith('```'):
        first_newline = text.find('\n')
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.rstrip().endswith('```'):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewerError(
            f'LLM returned malformed JSON: {exc.msg} at line {exc.lineno}'
        ) from exc
    if not isinstance(document, dict):
        raise ReviewerError(
            f'LLM review must be a JSON object, got '
            f'{type(document).__name__}')
    return document


def _policy_version() -> str:
    """Late import: ship_gate imports this module at its top level."""
    from asha.governance.ship_gate import POLICY_VERSION
    return POLICY_VERSION


def run_live_semantic_reviewer(root: Path, snapshot_id: str,
                               timeout_s: int) -> dict:
    """ReviewerFn entry point: one bounded LLM call -> review artifact.

    Raises ReviewerError on ANY failure (transport, timeout, rate limit,
    malformed output, unverifiable context, bad findings) -- ship_gate
    maps it to a failed check entry (EXECUTION_FAILED). The one thing
    that does NOT raise: a snapshot_id echo that differs from the one we
    sent is carried into the artifact verbatim so the gate's
    validate_artifact rejects it as INVALID_EVIDENCE.
    """
    config = _config_from_env(timeout_s)
    context = _review_context(root, config.context_budget)
    payload = {
        'model': config.model,
        'temperature': config.temperature,
        'max_tokens': config.max_tokens,
        'response_format': {'type': 'json_object'},
        'messages': [
            {'role': 'system', 'content': PROMPT_SYSTEM},
            {'role': 'user',
             'content': f'snapshot_id: {snapshot_id}\n\n{context}'},
        ],
    }
    try:
        raw = _http_post(_endpoint(config.base_url), payload, config)
    except ReviewerError:
        raise
    except urllib.error.HTTPError as exc:
        raise ReviewerError(
            f'LLM endpoint returned HTTP {exc.code} '
            f'({exc.reason}) -- fail closed') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReviewerError(
            f'LLM endpoint unreachable: {type(exc).__name__}: {exc} '
            '-- fail closed') from exc
    if not isinstance(raw, dict):
        raise ReviewerError(
            f'LLM transport must return an object, got '
            f'{type(raw).__name__}')
    document = _parse_json(_message_content(raw))
    echoed = document.get('snapshot_id')
    if not isinstance(echoed, str) or not echoed.strip():
        raise ReviewerError(
            'LLM response omitted the required snapshot_id echo '
            '(malformed output -- fail closed)')
    findings = document.get('findings')
    try:
        # strict Phase 2 schema: unknown severity, empty field, non-list
        # findings all raise here -> EXECUTION_FAILED at the gate.
        semantic_review.validate_findings(findings)
    except GateContractError as exc:
        raise ReviewerError(
            f'LLM findings violate the Phase 2 schema: {exc}') from exc
    usage = raw.get('usage') if isinstance(raw.get('usage'), dict) else None
    return {
        'schema': semantic_review.REVIEW_SCHEMA,
        # verbatim echo: a mismatching echo fails validate_artifact
        # (INVALID_EVIDENCE); the prompt carried the same id.
        'snapshot_id': echoed,
        'policy_version': _policy_version(),
        'status': 'live',
        'model': config.model,
        'findings': findings,
        'usage': usage,
    }
