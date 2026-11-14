"""Persistent default coding Skill for governed workers.

One optional user-level policy file decides, once, that every coding worker
driven by an agent CLI carries the project's mandated Skill, so a Spec author
never has to remember ``hermes -s clean-code`` per task.

Resolution order (first hit wins):

1. ``ASHA_WORKER_POLICY`` -- explicit path to a policy file (must exist).
2. ``$HOME/.config/asha/worker-policy.json`` -- user-level default.
3. none -- policy disabled; existing Specs keep byte-identical argv.

Policy file::

    {
      "coding_workers": {
        "enabled": true,
        "runner": "hermes",
        "skill": "clean-code",
        "executable": "hermes",
        "skill_path": null,
        "overlay_path": null
      }
    }

``overlay_path`` is an optional plain-text engineering-policy overlay (for
example "use the canonical Enum for fixed domain vocabulary"). When it is set
and readable, its text is prepended to the Agent CLI's one-shot prompt and its
digest is recorded -- an instruction layer, never a gate.

The Skill itself is never copied into this repository: the file is resolved
from the installed agent profile (``HERMES_HOME``, ``~/.hermes``,
``%LOCALAPPDATA%/hermes``, ``%APPDATA%/hermes``) or ``skill_path``. An enabled
policy that cannot resolve a readable Skill fails closed *before* the worker
starts -- a coding worker never runs silently without its mandated Skill.

Facts are reported honestly: ``SKILL_PRELOADED`` means the argv carries the
agent's documented preload flag and the Skill file was readable and hashed.
Nothing here claims the model applied the Skill; that is ``SKILL_APPLIED``,
which only a diff review can establish.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import OrchestratorError

#: Environment variable holding an explicit policy-file path.
POLICY_ENV = 'ASHA_WORKER_POLICY'

#: User-level policy default, relative to the home directory.
POLICY_RELATIVE = Path('.config') / 'asha' / 'worker-policy.json'

#: Skill and agent CLI the policy targets when the file omits them.
DEFAULT_SKILL = 'clean-code'
DEFAULT_RUNNER = 'hermes'

CODING = 'coding'
NON_CODING = 'non_coding'

#: Spec field that declares what a worker does; absent means coding.
TASK_KIND_FIELD = 'task_kind'

#: Agent CLI flags that preload Skills (documented Hermes interface).
_SKILL_FLAGS = ('-s', '--skills')
_ONESHOT_FLAGS = ('-z', '--oneshot')
_SKILL_DIR = 'skills'
_VERSION_RE = re.compile(r'^\s*(?:version:\s*"?([^"\r\n]+?)"?\s*$)',
                         re.MULTILINE)


class MandatorySkillUnavailable(OrchestratorError):
    """The configured mandatory Skill could not be resolved or read.

    Fail-closed: the worker is not started, so no unguarded coding run can be
    mistaken for a governed one.
    """


@dataclass(frozen=True)
class CodingSkillPolicy:
    """Effective policy after file/env resolution."""

    enabled: bool
    runner: str
    skill: str
    executable: str
    skill_path: str | None
    overlay_path: str | None
    source: str

    @classmethod
    def disabled(cls) -> CodingSkillPolicy:
        return cls(enabled=False, runner=DEFAULT_RUNNER, skill=DEFAULT_SKILL,
                   executable=DEFAULT_RUNNER, skill_path=None,
                   overlay_path=None, source='defaults:disabled')

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any],
                     source: str) -> CodingSkillPolicy:
        section = data.get('coding_workers')
        if not isinstance(section, Mapping):
            raise OrchestratorError(
                f'{source}: missing "coding_workers" object')
        enabled = section.get('enabled', False)
        if not isinstance(enabled, bool):
            raise OrchestratorError(
                f'{source}: coding_workers.enabled must be a boolean')
        runner = _text(section.get('runner'), DEFAULT_RUNNER, source,
                       'coding_workers.runner')
        skill = _text(section.get('skill'), DEFAULT_SKILL, source,
                      'coding_workers.skill')
        executable = _text(section.get('executable'), runner, source,
                           'coding_workers.executable')
        raw_path = section.get('skill_path')
        if raw_path is not None and not isinstance(raw_path, str):
            raise OrchestratorError(
                f'{source}: coding_workers.skill_path must be a string or null')
        raw_overlay = section.get('overlay_path')
        if raw_overlay is not None and not isinstance(raw_overlay, str):
            raise OrchestratorError(
                f'{source}: coding_workers.overlay_path must be a string '
                'or null')
        return cls(enabled=enabled, runner=runner, skill=skill,
                   executable=executable,
                   skill_path=_expand(raw_path) if raw_path else None,
                   overlay_path=_expand(raw_overlay) if raw_overlay else None,
                   source=source)


@dataclass(frozen=True)
class SkillFile:
    """A resolved, readable Skill with its provenance."""

    name: str
    path: Path
    version: str | None
    sha256: str


def _text(value: Any, fallback: str, source: str, field: str) -> str:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise OrchestratorError(f'{source}: {field} must be a non-empty string')
    return value.strip()


def _expand(raw: str) -> str:
    return os.path.expandvars(os.path.expanduser(raw))


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except OSError as exc:
        raise OrchestratorError(f'{path}: {exc}') from exc
    except json.JSONDecodeError as exc:
        raise OrchestratorError(f'{path}: invalid JSON ({exc})') from exc
    if not isinstance(data, Mapping):
        raise OrchestratorError(f'{path}: policy must be a JSON object')
    return data


def load_policy(environ: Mapping[str, str] | None = None,
                home: Path | None = None) -> CodingSkillPolicy:
    """Resolve the effective coding-Skill policy (never raises on absence)."""
    env = os.environ if environ is None else environ
    explicit = (env.get(POLICY_ENV) or '').strip()
    if explicit:
        path = Path(_expand(explicit))
        if not path.is_file():
            raise OrchestratorError(
                f'{POLICY_ENV} points at a missing policy file: {path}')
        return CodingSkillPolicy.from_mapping(_read_json(path), str(path))
    user_path = (Path(home) if home else Path.home()) / POLICY_RELATIVE
    if user_path.is_file():
        return CodingSkillPolicy.from_mapping(_read_json(user_path),
                                              str(user_path))
    return CodingSkillPolicy.disabled()


def task_kind(worker: Mapping[str, Any]) -> str:
    """Declared work kind; absent means coding (Asha's normal worker)."""
    raw = worker.get(TASK_KIND_FIELD)
    if raw is None:
        return CODING
    if not isinstance(raw, str):
        raise OrchestratorError(
            f'{worker.get("id")}.{TASK_KIND_FIELD} must be a string')
    value = raw.strip().lower()
    if value in (CODING, NON_CODING):
        return value
    raise OrchestratorError(
        f'{worker.get("id")}.{TASK_KIND_FIELD}={raw!r} '
        f'(expected {CODING!r} or {NON_CODING!r})')


def _profile_roots(environ: Mapping[str, str]) -> list[Path]:
    """Installed agent profile roots, most specific first."""
    roots: list[Path] = []
    for key in ('HERMES_HOME',):
        value = (environ.get(key) or '').strip()
        if value:
            roots.append(Path(_expand(value)))
    roots.append(Path.home() / '.hermes')
    for key in ('LOCALAPPDATA', 'APPDATA'):
        value = (environ.get(key) or '').strip()
        if value:
            roots.append(Path(value) / 'hermes')
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _version_of(text: str) -> str | None:
    """First non-empty ``version:`` scalar in the Skill frontmatter."""
    for match in _VERSION_RE.finditer(text):
        value = match.group(1).strip()
        if value:
            return value
    return None


def resolve_skill(policy: CodingSkillPolicy,
                  environ: Mapping[str, str] | None = None) -> SkillFile:
    """Locate and hash the mandated Skill, or fail closed."""
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    if policy.skill_path:
        configured = Path(policy.skill_path)
        candidates.append(configured / 'SKILL.md'
                          if configured.is_dir() else configured)
    for root in _profile_roots(env):
        candidates.append(root / _SKILL_DIR / policy.skill / 'SKILL.md')
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise MandatorySkillUnavailable(
                f'mandatory skill {policy.skill!r} is unreadable at {path}: '
                f'{exc}') from exc
        return SkillFile(name=policy.skill, path=path,
                         version=_version_of(text), sha256=digest)
    searched = ', '.join(str(candidate) for candidate in candidates)
    raise MandatorySkillUnavailable(
        f'mandatory skill {policy.skill!r} not found (searched: {searched}); '
        'install it into the agent profile or point '
        'coding_workers.skill_path at SKILL.md')


def _is_target_cli(argv: list[str], policy: CodingSkillPolicy) -> bool:
    """True when argv[0] names the configured agent CLI (basename match)."""
    if not argv:
        return False
    invoked = Path(argv[0]).stem.lower()
    return invoked == Path(policy.executable).stem.lower()


def _skill_names(value: str) -> list[str]:
    return [part.strip() for part in value.split(',') if part.strip()]


def inject_skill(argv: list[str], skill: str) -> tuple[list[str], bool]:
    """Add ``skill`` to the argv's preload flags without duplicating it.

    Returns (argv, newly_added). ``newly_added`` is False both when the Skill
    was already requested and when nothing changed -- callers report what the
    argv carries, not what they wished it carried.
    """
    out = list(argv)
    for index, token in enumerate(out):
        if token in _SKILL_FLAGS and index + 1 < len(out):
            names = _skill_names(out[index + 1])
            if skill in names:
                return out, False
            out[index + 1] = ','.join([*names, skill])
            return out, True
        if token.startswith('--skills='):
            names = _skill_names(token.split('=', 1)[1])
            if skill in names:
                return out, False
            out[index] = '--skills=' + ','.join([*names, skill])
            return out, True
    # No preload flag: put it in front of the one-shot prompt so the prompt
    # stays the flag's value (flag order after -z is a CLI error).
    for flag in _ONESHOT_FLAGS:
        if flag in out:
            position = out.index(flag)
            out[position:position] = ['-s', skill]
            return out, True
    return out + ['-s', skill], True


def _overlay_text(policy: CodingSkillPolicy) -> tuple[str, str] | None:
    """(text, sha256) of the configured engineering overlay, or None."""
    if not policy.overlay_path:
        return None
    path = Path(policy.overlay_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MandatorySkillUnavailable(
            f'engineering overlay unreadable at {path}: {exc}') from exc
    text = raw.decode('utf-8').strip()
    if not text:
        raise MandatorySkillUnavailable(
            f'engineering overlay at {path} is empty')
    return text, hashlib.sha256(raw).hexdigest()


def apply_overlay(argv: list[str], text: str) -> tuple[list[str], bool]:
    """Prepend the overlay to the one-shot prompt; never a shell string."""
    out = list(argv)
    for index, token in enumerate(out):
        if token in _ONESHOT_FLAGS and index + 1 < len(out):
            out[index + 1] = f'{text}\n\n---\n\n{out[index + 1]}'
            return out, True
        if token.startswith('--oneshot='):
            out[index] = f'--oneshot={text}\n\n---\n\n{token.split("=", 1)[1]}'
            return out, True
    raise OrchestratorError(
        'coding_workers.overlay_path is configured but the worker argv has no '
        'one-shot prompt flag to carry it')


def prepare(worker: Mapping[str, Any], argv: list[str], runner_name: str,
            policy: CodingSkillPolicy | None = None,
            environ: Mapping[str, str] | None = None
            ) -> tuple[list[str], dict[str, Any]]:
    """Apply the policy to one worker's primary argv.

    Only the configured agent CLI is touched; every other runner keeps its
    argv byte-for-byte. Metadata is additive and describes what was actually
    resolved and injected.
    """
    effective = load_policy(environ) if policy is None else policy
    if not effective.enabled or runner_name != 'command':
        return argv, {}
    kind = task_kind(worker)
    if kind != CODING or not _is_target_cli(argv, effective):
        return argv, {}
    skill = resolve_skill(effective, environ)
    final_argv, added = inject_skill(argv, skill.name)
    facts: dict[str, Any] = {
        'task_kind': kind,
        'skill_policy_source': effective.source,
        'skill_name': skill.name,
        'skill_version': skill.version,
        'skill_path': str(skill.path),
        'skill_source_sha256': skill.sha256,
        'skill_preload_enabled': True,
        'skill_preloaded': True,
        'skill_preload_added': added,
    }
    overlay = _overlay_text(effective)
    if overlay is not None:
        text, digest = overlay
        final_argv, facts['policy_overlay_applied'] = apply_overlay(
            final_argv, text)
        facts['policy_overlay_path'] = effective.overlay_path
        facts['policy_overlay_sha256'] = digest
    facts['worker_command'] = list(final_argv)
    return final_argv, facts
