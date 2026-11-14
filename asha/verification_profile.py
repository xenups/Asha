"""Trusted operator verification profiles (0.7.4).

The scope matrix (``scope_resolver.SCOPE_CHECKS``) is a repository-agnostic
policy: S2+ always demands ruff + pytest + mypy. A project that configures
neither ruff nor mypy is then permanently BLOCKED, because a missing tool is
recorded as a skipped required check and a skip is never a pass -- correct
fail-closed behaviour, applied to a question the project never answered.

A profile answers that question explicitly and audibly. It is:

* **operator-owned** -- loaded from ``ASHA_VERIFICATION_PROFILE`` (a path), never
  from a task spec, so a worker cannot widen or narrow its own matrix;
* **repository-bound** -- it names the git remotes it applies to, and loading
  fails closed on any other repository;
* **digest-bound** -- loaded once, hashed, and re-hashed before sealing, so a
  candidate tree that edits the profile cannot change the run it is in;
* **declarative** -- a check is either REQUIRED (argv to execute) or
  explicitly NOT_CONFIGURED (with provenance). A missing executable is never
  turned into NOT_CONFIGURED: unconfigured is a claim someone signed, not an
  observation about a broken environment.

Absent a valid profile the scope matrix applies unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROFILE_ENV = 'ASHA_VERIFICATION_PROFILE'
SCHEMA_VERSION = 1
#: Entry status for a check the project explicitly does not gate on. Distinct
#: from 'skipped' (a required check that could not run) by construction.
NOT_CONFIGURED = 'not_configured'


class ProfileError(RuntimeError):
    """Untrusted, malformed, mismatched or mutated profile (fail closed)."""


@dataclass(frozen=True)
class ServicePlan:
    """Authoritative checks for one service root."""

    label: str
    root: str
    checks: tuple[tuple[str, tuple[str, ...]], ...]
    not_configured: tuple[tuple[str, str], ...]
    timeout_s: float
    python: str | None


@dataclass(frozen=True)
class Profile:
    """A loaded, identity-checked, digest-bound verification profile."""

    id: str
    schema: int
    remotes: tuple[str, ...]
    services: tuple[ServicePlan, ...]
    ci_workflows: tuple[str, ...]
    provenance: dict
    digest: str
    source: str

    def service_for(self, label: str) -> ServicePlan:
        """The plan for a service label, or fail closed.

        A diff that reaches an undeclared service is not silently verified by
        the scope matrix: that would mix two policies in one comparison.
        """
        for plan in self.services:
            if plan.label == label:
                return plan
        raise ProfileError(
            f'profile {self.id!r} declares no checks for service {label!r}: '
            'a profiled run cannot fall back to the scope matrix')


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProfileError(message)


def _text(value: object, field: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()),
             f'{field} must be a non-empty string')
    return str(value).strip()


def _str_list(value: object, field: str, *, allow_empty: bool = False
              ) -> tuple[str, ...]:
    """A validated list of non-empty strings (schema leaves)."""
    items = value if isinstance(value, list) else None
    _require(items is not None and (allow_empty or bool(items)),
             f'{field} must be a {"list" if allow_empty else "non-empty list"}')
    out: list[str] = []
    for item in items or []:
        _require(isinstance(item, str) and item.strip() != '',
                 f'{field} entries must be non-empty strings')
        out.append(str(item))
    return tuple(out)


def _argv(value: object, field: str) -> tuple[str, ...]:
    return _str_list(value, field)


def _service(label: str, payload: object) -> ServicePlan:
    _require(isinstance(payload, dict), f'services.{label} must be an object')
    data = payload if isinstance(payload, dict) else {}
    checks_raw = data.get('checks')
    _require(isinstance(checks_raw, dict) and bool(checks_raw),
             f'services.{label}.checks must be a non-empty object')
    checks: list[tuple[str, tuple[str, ...]]] = []
    for name, argv in sorted((checks_raw or {}).items()):
        checks.append((_text(name, f'services.{label}.checks key'),
                       _argv(argv, f'services.{label}.checks.{name}')))
    configured_raw = data.get('not_configured') or {}
    _require(isinstance(configured_raw, dict),
             f'services.{label}.not_configured must be an object')
    configured: list[tuple[str, str]] = []
    for name, provenance in sorted(configured_raw.items()):
        configured.append((_text(name, f'services.{label}.not_configured key'),
                           _text(provenance,
                                 f'services.{label}.not_configured.{name}')))
    overlap = {name for name, _ in configured} & {name for name, _ in checks}
    _require(not overlap,
             f'services.{label}: check(s) declared both required and '
             f'not_configured: {", ".join(sorted(overlap))}')
    timeout_raw = data.get('timeout_s', 900)
    _require(isinstance(timeout_raw, (int, float)) and timeout_raw > 0,
             f'services.{label}.timeout_s must be a positive number')
    python = data.get('python')
    if python is not None:
        python = _text(python, f'services.{label}.python')
    return ServicePlan(
        label=label,
        root=_text(data.get('root', label), f'services.{label}.root'),
        checks=tuple(checks),
        not_configured=tuple(configured),
        timeout_s=float(timeout_raw),
        python=python,
    )


def _parse(raw: bytes, source: str) -> Profile:
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProfileError(f'profile {source} is not valid JSON: {exc}') from exc
    _require(isinstance(payload, dict), 'profile must be a JSON object')
    data = payload if isinstance(payload, dict) else {}
    _require(data.get('schema') == SCHEMA_VERSION,
             f'profile schema must be {SCHEMA_VERSION}')
    repository = data.get('repository')
    _require(isinstance(repository, dict),
             'repository must be an object naming the remotes it binds to')
    repo = repository if isinstance(repository, dict) else {}
    remotes = tuple(sorted({
        _normalise_remote(item)
        for item in _str_list(repo.get('remotes'), 'repository.remotes')}))
    services_raw = data.get('services')
    _require(isinstance(services_raw, dict) and bool(services_raw),
             'services must be a non-empty object')
    services = tuple(
        _service(str(label), value)
        for label, value in sorted((services_raw or {}).items()))
    workflows = _str_list(repo.get('ci_workflows') or [],
                          'repository.ci_workflows', allow_empty=True)
    provenance = data.get('provenance') or {}
    _require(isinstance(provenance, dict), 'provenance must be an object')
    return Profile(
        id=_text(data.get('id'), 'id'),
        schema=SCHEMA_VERSION,
        remotes=remotes,
        services=services,
        ci_workflows=workflows,
        provenance=provenance if isinstance(provenance, dict) else {},
        digest=hashlib.sha256(raw).hexdigest(),
        source=source,
    )


def _normalise_remote(url: str) -> str:
    """Comparable repository identity: host/path, no scheme, no ``.git``.

    ``git@github.com:o/r.git``, ``https://github.com/o/r`` and
    ``ssh://git@github.com/o/r.git`` are one identity; nothing else is.
    """
    text = url.strip().lower()
    for prefix in ('ssh://', 'git://', 'https://', 'http://'):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.startswith('git@') or ('@' in text.split('/', 1)[0]
                                   and ':' in text.split('/', 1)[0]):
        text = text.split('@', 1)[1]
    text = text.replace(':', '/', 1) if ':' in text.split('/', 1)[0] else text
    text = text.rstrip('/')
    return text.removesuffix('.git')


def load(path: Path | str) -> Profile:
    """Read and validate a profile file, recording its raw-byte digest."""
    source = Path(path).expanduser()
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ProfileError(f'cannot read profile {source}: {exc}') from exc
    _require(bool(raw.strip()), f'profile {source} is empty')
    return _parse(raw, str(source))


def _git_remotes(root: Path) -> list[str]:
    try:
        proc = subprocess.run(['git', 'remote', 'get-url', '--all', 'origin'],
                              cwd=str(root), capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProfileError(f'cannot read git remotes: {exc}') from exc
    if proc.returncode != 0:
        raise ProfileError(
            'cannot read git remotes (no origin?): '
            + (proc.stderr or '').strip()[-200:])
    return [line for line in (proc.stdout or '').splitlines() if line.strip()]


def verify_repository(profile: Profile, root: Path) -> None:
    """Fail closed unless the repository is the one the profile binds to."""
    observed = {_normalise_remote(url) for url in _git_remotes(Path(root))}
    if not observed & set(profile.remotes):
        raise ProfileError(
            'repository identity mismatch: profile binds '
            + ', '.join(profile.remotes)
            + '; repository origin is '
            + (', '.join(sorted(observed)) or '(none)'))


def load_selected(root: Path, environ: dict[str, str] | None = None
                  ) -> Profile | None:
    """The operator-selected profile for ``root``, or None when unset.

    None is the historical behaviour (scope matrix). Every failure raises:
    a profile that cannot be loaded must never degrade into the default
    matrix, because that would silently verify something else.
    """
    env = os.environ if environ is None else environ
    selected = (env.get(PROFILE_ENV) or '').strip()
    if not selected:
        return None
    profile = load(selected)
    verify_repository(profile, Path(root))
    return profile


def verify_unchanged(profile: Profile) -> None:
    """Fail closed if the profile bytes changed after the snapshot."""
    current = load(profile.source)
    if current.digest != profile.digest:
        raise ProfileError(
            f'profile {profile.id!r} changed during the run '
            f'({profile.digest[:12]} -> {current.digest[:12]}): the matrix a '
            'run was verified under cannot be edited mid-flight')


def statement(profile: Profile) -> dict:
    """Evidence record: which profile, which digest, which provenance."""
    return {
        'id': profile.id,
        'schema': profile.schema,
        'digest': profile.digest,
        'source': profile.source,
        'remotes': list(profile.remotes),
        'ci_workflows': list(profile.ci_workflows),
        'provenance': profile.provenance,
        'services': {plan.label: {
            'required': [name for name, _ in plan.checks],
            'not_configured': [name for name, _ in plan.not_configured],
            'timeout_s': plan.timeout_s,
        } for plan in profile.services},
    }
