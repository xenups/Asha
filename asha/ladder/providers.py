"""Provider boundary for Ladder stages.

One transport exists in Asha (``asha.governance.live_reviewer``); this
module adapts it to a typed call, classifies its failures, and refuses to
invent identities. There is deliberately no fake provider here: production
entry points can only resolve a real, configured provider, and an
unconfigured environment resolves to :class:`ProviderUnavailable`, which
the orchestrator records as ``BLOCKED``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from asha.governance import live_reviewer
from asha.ladder.models import LadderError
from asha.ladder.policy import ReviewPolicy

#: Environment contract inherited from the existing live reviewer.
API_KEY_ENV = 'ASHA_LLM_API_KEY'
BASE_URL_ENV = 'ASHA_LLM_BASE_URL'
MODEL_ENV = 'ASHA_LLM_MODEL'

#: Per-call operation budget; the task Deadline caps it further.
OPERATION_TIMEOUT_S = 120


class ProviderUnavailable(LadderError):
    """No usable provider: missing configuration or rejected credentials."""


class ProviderTimeout(LadderError):
    """The provider call hit its budget (distinct from a malformed reply)."""


class ProviderError(LadderError):
    """The provider ran and failed: transport error, HTTP error, bad body."""


@dataclass(frozen=True)
class Completion:
    """One provider reply plus the metadata the provider actually reported."""

    text: str
    provider: str
    model: str
    session_id: str = ''

    def as_dict(self) -> dict[str, str]:
        return {
            'provider': self.provider,
            'model': self.model,
            'session_id': self.session_id,
        }


class Provider(Protocol):
    """What a Ladder stage needs from a provider."""

    @property
    def name(self) -> str:
        """Provider identity (host/service), never a guess."""

    @property
    def model(self) -> str:
        """Configured model identity, never a guess."""

    def complete(self, system_prompt: str, user_content: str, *,
                 timeout_s: int) -> Completion:
        """Run one isolated completion; raise a typed failure otherwise."""


@dataclass(frozen=True)
class LiveProvider:
    """Adapter over the existing HTTP transport in ``live_reviewer``.

    Each ``complete`` call is a separate request with its own context: no
    conversation is accumulated between stages, so the two review passes
    cannot see each other's output.
    """

    provider: str
    model: str

    @property
    def name(self) -> str:
        return self.provider

    def complete(self, system_prompt: str, user_content: str, *,
                 timeout_s: int) -> Completion:
        try:
            reply = live_reviewer.complete(
                system_prompt, user_content, timeout_s=int(timeout_s))
        except TimeoutError as exc:
            raise ProviderTimeout(
                f'provider did not answer within {int(timeout_s)}s') from exc
        except live_reviewer.ReviewerAuthError as exc:
            raise ProviderUnavailable(
                'provider rejected the configured credentials') from exc
        except live_reviewer.ReviewerError as exc:
            raise ProviderError(str(exc)) from exc
        text = reply.get('content')
        if not isinstance(text, str) or not text.strip():
            raise ProviderError('provider returned an empty message')
        return Completion(
            text=text,
            provider=str(reply.get('provider') or self.provider),
            model=str(reply.get('model') or self.model),
            session_id='',
        )


def provider_identity(base_url: str) -> str:
    """Provider id from the configured endpoint host (never fabricated)."""
    host = urlparse(base_url).hostname
    return host or base_url.rstrip('/')


def resolve_provider(*, env: dict[str, str] | None = None) -> LiveProvider:
    """Resolve the real provider; unavailable configuration fails closed."""
    source = os.environ if env is None else env
    if not source.get(API_KEY_ENV, '').strip():
        raise ProviderUnavailable(
            f'{API_KEY_ENV} is not set: Ladder needs a configured review '
            'provider. Set ASHA_LLM_API_KEY (plus optional '
            f'{BASE_URL_ENV}/{MODEL_ENV}) and re-run; nothing is reviewed '
            'without it and no approval is implied.')
    base_url = (source.get(BASE_URL_ENV, '') or
                live_reviewer.DEFAULT_BASE_URL).strip().rstrip('/')
    model = (source.get(MODEL_ENV, '') or
             live_reviewer.DEFAULT_MODEL).strip()
    return LiveProvider(provider=provider_identity(base_url), model=model)


def independence_hint(policy: ReviewPolicy) -> str:
    """Human-readable statement of the independence the policy demands."""
    return (f'policy {policy.policy_id} requires '
            f'{policy.required_independence.value} independence')
