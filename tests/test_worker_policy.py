"""The persistent default coding-Skill policy (worker_policy).

Covers the four things the policy must decide honestly:

* a disabled/absent policy leaves every argv byte-identical;
* an enabled policy preloads the mandated Skill into the configured Agent
  CLI only, never into another runner and never into non-coding work;
* a mandated-but-missing Skill fails closed before the worker starts;
* the recorded facts describe what the argv and files actually carried.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asha import runner as runner_mod
from asha import worker_policy
from asha.types import OrchestratorError
from asha.worker_policy import (
    CODING,
    NON_CODING,
    CodingSkillPolicy,
    MandatorySkillUnavailable,
)

SKILL_TEXT = (
    "---\n"
    "name: clean-code\n"
    "metadata:\n"
    "  version: \"4.0.0\"\n"
    "---\n"
    "\n"
    "# Clean Code And Clean Architecture\n"
)


def _policy_file(tmp_path: Path, **section: object) -> Path:
    payload = {"coding_workers": {"enabled": True, "runner": "hermes",
                                  "skill": "clean-code", **section}}
    path = tmp_path / "worker-policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _profile(tmp_path: Path, name: str = "clean-code",
             text: str = SKILL_TEXT) -> Path:
    root = tmp_path / "profile"
    skill = root / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(text, encoding="utf-8")
    return root


def test_absent_policy_is_disabled_and_leaves_argv_alone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(worker_policy.POLICY_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    policy = worker_policy.load_policy()
    assert policy.enabled is False
    argv = ["hermes", "-z", "do work"]
    assert worker_policy.prepare({"id": "w"}, argv, "command") == (argv, {})


def test_enabled_policy_preloads_the_skill_before_the_prompt(
        tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    policy = CodingSkillPolicy.from_mapping(
        json.loads(_policy_file(tmp_path).read_text(encoding="utf-8")),
        "test")
    argv, facts = worker_policy.prepare(
        {"id": "w"}, ["hermes", "-z", "do work"], "command",
        policy=policy, environ={"HERMES_HOME": str(profile)})
    assert argv == ["hermes", "-s", "clean-code", "-z", "do work"]
    assert facts["skill_preloaded"] is True
    assert facts["skill_preload_added"] is True
    assert facts["skill_name"] == "clean-code"
    assert facts["skill_version"] == "4.0.0"
    assert len(facts["skill_source_sha256"]) == 64
    assert facts["worker_command"] == argv


def test_skill_already_requested_is_not_duplicated(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    policy = CodingSkillPolicy.from_mapping(
        json.loads(_policy_file(tmp_path).read_text(encoding="utf-8")),
        "test")
    argv, facts = worker_policy.prepare(
        {"id": "w"}, ["hermes", "-s", "clean-code", "-z", "do work"],
        "command", policy=policy, environ={"HERMES_HOME": str(profile)})
    assert argv.count("clean-code") == 1
    assert facts["skill_preload_added"] is False


def test_skill_is_appended_to_an_existing_preload_list(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    policy = CodingSkillPolicy.from_mapping(
        json.loads(_policy_file(tmp_path).read_text(encoding="utf-8")),
        "test")
    argv, _facts = worker_policy.prepare(
        {"id": "w"}, ["hermes", "--skills", "notes", "-z", "x"], "command",
        policy=policy, environ={"HERMES_HOME": str(profile)})
    assert argv[:3] == ["hermes", "--skills", "notes,clean-code"]


def test_non_coding_worker_opts_out(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    policy = CodingSkillPolicy.from_mapping(
        json.loads(_policy_file(tmp_path).read_text(encoding="utf-8")),
        "test")
    argv, facts = worker_policy.prepare(
        {"id": "w", "task_kind": NON_CODING}, ["hermes", "-z", "explain"],
        "command", policy=policy, environ={"HERMES_HOME": str(profile)})
    assert argv == ["hermes", "-z", "explain"]
    assert facts == {}
    assert worker_policy.task_kind({"id": "w"}) == CODING


def test_other_runners_keep_their_argv(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    policy = CodingSkillPolicy.from_mapping(
        json.loads(_policy_file(tmp_path).read_text(encoding="utf-8")),
        "test")
    for argv in (["python", "tool.py"], ["antigravity", "--task", "x"]):
        assert worker_policy.prepare({"id": "w"}, argv, "command",
                                     policy=policy,
                                     environ={"HERMES_HOME": str(profile)}
                                     ) == (argv, {})
    assert worker_policy.prepare({"id": "w"}, ["hermes", "-z", "x"],
                                 "antigravity", policy=policy,
                                 environ={"HERMES_HOME": str(profile)}
                                 ) == (["hermes", "-z", "x"], {})


def test_missing_mandatory_skill_fails_closed(tmp_path: Path) -> None:
    policy = CodingSkillPolicy.from_mapping(
        json.loads(_policy_file(tmp_path).read_text(encoding="utf-8")),
        "test")
    with pytest.raises(MandatorySkillUnavailable) as excinfo:
        worker_policy.prepare({"id": "w"}, ["hermes", "-z", "x"], "command",
                              policy=policy,
                              environ={"HERMES_HOME": str(tmp_path / "none")})
    assert "clean-code" in str(excinfo.value)
    assert "skill_path" in str(excinfo.value)


def test_explicit_policy_path_must_exist(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(worker_policy.POLICY_ENV, str(tmp_path / "absent.json"))
    with pytest.raises(OrchestratorError):
        worker_policy.load_policy()


def test_engineering_overlay_rides_on_the_prompt(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    overlay = tmp_path / "coding-overlay.md"
    overlay.write_text("Use the canonical Enum for domain states.\n",
                       encoding="utf-8")
    policy = CodingSkillPolicy.from_mapping(
        json.loads(_policy_file(tmp_path).read_text(encoding="utf-8")),
        "test")
    policy = CodingSkillPolicy(**{**policy.__dict__,
                                 "overlay_path": str(overlay)})
    argv, facts = worker_policy.prepare(
        {"id": "w"}, ["hermes", "-z", "refactor it"], "command",
        policy=policy, environ={"HERMES_HOME": str(profile)})
    prompt = argv[argv.index("-z") + 1]
    assert prompt.startswith("Use the canonical Enum")
    assert prompt.endswith("refactor it")
    assert facts["policy_overlay_applied"] is True
    assert len(facts["policy_overlay_sha256"]) == 64


def test_unknown_task_kind_is_refused(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    policy = CodingSkillPolicy.from_mapping(
        json.loads(_policy_file(tmp_path).read_text(encoding="utf-8")),
        "test")
    with pytest.raises(OrchestratorError):
        worker_policy.prepare({"id": "w", "task_kind": "gardening"},
                              ["hermes", "-z", "x"], "command",
                              policy=policy,
                              environ={"HERMES_HOME": str(profile)})


def test_runner_reports_skill_facts_in_its_audit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The facts reach the runner audit that evidence is built from."""
    profile = _profile(tmp_path)
    policy_path = _policy_file(tmp_path)
    monkeypatch.setenv(worker_policy.POLICY_ENV, str(policy_path))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    seen: list[list[str]] = []

    def fake_spawn(argv, cwd, timeout, env=None, deadline=None):
        seen.append(list(argv))
        return 0, "ok", ""

    monkeypatch.setattr(runner_mod, "_spawn", fake_spawn)
    result = runner_mod.CommandRunner().execute(
        {"id": "w", "cmd": ["hermes", "-z", "refactor"]}, tmp_path, 5)
    assert seen[0] == ["hermes", "-s", "clean-code", "-z", "refactor"]
    assert result.audit_metadata["skill_preloaded"] is True
    assert result.audit_metadata["task_kind"] == CODING
    assert result.audit_metadata["skill_source_sha256"]
