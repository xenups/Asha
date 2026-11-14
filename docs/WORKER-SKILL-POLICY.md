# Default coding Skill for workers

Asha starts workers as **subprocesses**, not as agent sessions it owns: the
runner is whatever the Spec's `cmd` (or `prompt`, for the Antigravity adapter)
names. A Worker therefore inherits an agent's rules only because it *is* that
agent. This page documents the one supported way to make that inheritance
mandatory and persistent.

## Configure once

Create `~/.config/asha/worker-policy.json` (Windows:
`C:\Users\<you>\.config\asha\worker-policy.json`):

```json
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
```

| Field | Meaning |
|---|---|
| `enabled` | `false`/absent = no policy at all: every Spec keeps byte-identical argv |
| `runner` | agent CLI the policy targets; only `hermes` has a documented preload flag today |
| `skill` | Skill name to preload (resolved inside the active profile, never copied into Asha) |
| `executable` | binary name whose argv is rewritten (`hermes`, `hermes.exe`, an absolute path) |
| `skill_path` | optional explicit `SKILL.md` path, when the profile is somewhere unusual |
| `overlay_path` | optional plain-text engineering policy prepended to the task prompt |

`ASHA_WORKER_POLICY=/path/to/policy.json` overrides the file location and must
exist — a policy path that does not is an error, not a silent fallback.

## What happens to a worker

For a `command` worker whose `cmd[0]` is the configured executable, and whose
`task_kind` is `coding` (the default), Asha:

1. resolves `SKILL.md` from `HERMES_HOME`, `~/.hermes`,
   `%LOCALAPPDATA%/hermes` or `%APPDATA%/hermes` (or `skill_path`);
2. hashes it and reads its `version:`;
3. inserts `-s <skill>` before the one-shot prompt, or appends the Skill to an
   existing `-s/--skills` value (never duplicated);
4. optionally prepends `overlay_path` text to the prompt;
5. records the facts below in the runner audit, which lands in sealed evidence
   under `skill_preload`.

Effective invocation: `hermes -s clean-code -z "<task>"`.

Nothing else changes: another runner (`antigravity`, a shell script, a plain
`python` command) keeps its argv byte-for-byte, and a Spec that already asks for
the Skill is left alone.

## Opt out for non-coding work

```json
{"id": "explain", "cmd": ["hermes", "-z", "Explain the difference between UTC and local time."], "task_kind": "non_coding"}
```

`task_kind` accepts `coding` (default) or `non_coding`; any other value is
rejected before dispatch. Explicit classification, not prompt guesswork.

## Fails closed when the Skill is missing

If the policy is enabled and the Skill cannot be resolved or read, the worker is
**not started**. The run reports

```
state:  BLOCKED
reason: mandatory_skill_unavailable
```

with a message naming the searched paths. Restore the Skill
(`hermes skills install btseee/clean-code-skills/skills/clean-code`) or point
`skill_path` at the file.

## Evidence

Sealed worker evidence grows one block:

```json
"skill_preload": {
  "task_kind": "coding",
  "skill_policy_source": "C:\\Users\\me\\.config\\asha\\worker-policy.json",
  "skill_name": "clean-code",
  "skill_version": "4.0.0",
  "skill_path": "C:\\Users\\me\\AppData\\Local\\hermes\\skills\\clean-code\\SKILL.md",
  "skill_source_sha256": "a37a3953…",
  "skill_preload_enabled": true,
  "skill_preloaded": true,
  "skill_preload_added": true,
  "policy_overlay_applied": false,
  "worker_command": ["hermes", "-s", "clean-code", "-z", "…"]
}
```

## The five different claims

| Claim | Meaning | Who proves it |
|---|---|---|
| `SKILL_CONFIGURED` | policy file enables a Skill | the policy file |
| `SKILL_AVAILABLE` | `SKILL.md` exists, is readable, hashed | `skill_path` / `skill_source_sha256` |
| `SKILL_PRELOADED` | the argv carried the agent's preload flag | `worker_command`, `skill_preloaded` |
| `SKILL_APPLIED` | the produced code follows the rules | a human/diff review of the candidate |
| `SKILL_ENFORCED` | a run is refused without it | *not implemented* — see below |

Asha gates on the first three. It does **not** enforce the fourth: a Worker can
be handed the full Skill and still write code that ignores it. Treating
`skill_preloaded` as `skill_applied` is exactly the error this table prevents.

## Acceptance, and why it is a separate gate

`verify_command` is executed by Asha after a zero worker exit, and its real exit
code becomes the worker's exit code (`NOT_SUPPLIED`, `NOT_EXECUTED`,
`EXECUTED_AND_PASSED`, `EXECUTED_AND_FAILED`, `INCONCLUSIVE` — zero tests
collected or only skips/deselects is never a pass). A worker whose supplied
acceptance did not demonstrably pass is `FAILED / acceptance_failed` and its
evidence is not sealed, whatever the primary exit code was.

Backend differences:

| Backend | Skill preload | Acceptance | Notes |
|---|---|---|---|
| `command` + `hermes` | yes (`-s`, policy or Spec) | yes, audited | full Skill text is in the prompt before the first model action |
| `command` (other binary) | only if the binary has its own flag; Asha never guesses | yes, audited | argv untouched |
| `antigravity` | no equivalent documented flag | yes, via `--task` text | the task text carries the acceptance command; the Skill does not |

Worktrees are **not** security sandboxes: a worker has full filesystem access
inside its worktree, and scope is checked against the observed diff afterwards.
