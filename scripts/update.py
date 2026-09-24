#!/usr/bin/env python3
"""Asha-Harness atomic, fail-closed self-update mechanism.

Protocol (all 5 steps, fail-closed at every boundary):
  1. Clean working tree guard   : `git status --porcelain` must be empty,
                                  else refuse (exit 2).
  2. Fetch & diff inspection    : `git fetch <remote> <branch>`; no new
                                  commits -> "Asha is already up to date."
                                  exit 0.
  3. Fast-forward update        : `git merge --ff-only <remote>/<branch>`.
  4. Dependency & ABI audit     : code_search.py --verify-env, ruff, pytest.
  5. Rollback on gate failure   : any red gate -> `git reset --hard HEAD@{1}`
                                  + exit 1 with the exact failure reason.

--dry-run performs steps 1-2 and REPORTS whether an update would apply,
without mutating the tree.

Submodule repos registered in .jspace/dependencies.json are updated with the
same protocol (fetch -> ff-only merge -> audit); the audit suite is the
harness's own tests for the self update and the submodule's own test command
(if declared) for dependencies. (None are registered in the default config.)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPS = ROOT / ".jspace" / "dependencies.json"
CODE_SEARCH = ROOT / "asha" / "code_search.py"
VENV_PY = Path(sys.executable)

RUFF_ARGS = ["-m", "ruff", "check", "."]
PYTEST_ARGS = ["-m", "pytest", "tests/", "-q"]


class UpdateError(Exception):
    pass


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True, timeout=300)


def git(*args: str) -> subprocess.CompletedProcess:
    return run(["git", *args])


def load_deps() -> dict:
    if not DEPS.is_file():
        raise UpdateError(f"missing {DEPS.relative_to(ROOT)}")
    try:
        data = json.loads(DEPS.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise UpdateError(f"cannot parse {DEPS.relative_to(ROOT)}: {exc}") from exc
    self_cfg = data.get("self", {})
    if not isinstance(self_cfg, dict) or not self_cfg.get("remote") or not self_cfg.get("branch"):
        raise UpdateError("dependencies.json: self needs remote + branch")
    submodules = data.get("submodules", [])
    if not isinstance(submodules, list):
        raise UpdateError("dependencies.json: submodules must be a list")
    return data


def clean_tree_guard() -> None:
    status = git("status", "--porcelain")
    if status.returncode != 0:
        raise UpdateError("git status failed: " + status.stderr.strip())
    if status.stdout.strip():
        raise UpdateError(
            "working tree is dirty; refusing to update (fail-closed). "
            "Commit or stash first:\n" + status.stdout.strip()
        )


def fetch_remote(remote: str, branch: str) -> subprocess.CompletedProcess:
    proc = git("fetch", remote, branch)
    if proc.returncode != 0:
        raise UpdateError(f"git fetch {remote} {branch} failed: {proc.stderr.strip()}")
    return proc


def incoming_commits(remote: str, branch: str) -> list[str]:
    local = git("rev-parse", "HEAD")
    remote_ref = git("rev-parse", f"{remote}/{branch}")
    if local.returncode != 0 or remote_ref.returncode != 0:
        raise UpdateError("cannot resolve local HEAD or remote ref")
    if local.stdout.strip() == remote_ref.stdout.strip():
        return []
    proc = git("log", "--oneline", f"HEAD..{remote}/{branch}")
    return proc.stdout.splitlines()


def audit_self(dry_run: bool) -> None:
    steps: list[tuple[str, list[str]]] = [
        ("verify-env", [str(VENV_PY), str(CODE_SEARCH), "--verify-env"]),
        ("ruff", [str(VENV_PY), *RUFF_ARGS]),
        ("pytest", [str(VENV_PY), *PYTEST_ARGS]),
    ]
    for name, cmd in steps:
        proc = run(cmd)
        if proc.returncode != 0:
            tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-8:])
            raise UpdateError(f"gate '{name}' FAILED (exit {proc.returncode}):\n{tail}")


def rollback() -> None:
    proc = git("reset", "--hard", "HEAD@{1}")
    if proc.returncode != 0:
        print(f"CRITICAL: rollback failed: {proc.stderr.strip()}", file=sys.stderr)
    else:
        print("rolled back to HEAD@{1} (pre-update commit)")


def update_self(dry_run: bool) -> bool:
    deps = load_deps()
    self_cfg = deps["self"]
    remote, branch = self_cfg["remote"], self_cfg["branch"]

    print(f"[asha-update] self: {remote}/{branch}")
    clean_tree_guard()

    fetch_remote(remote, branch)
    incoming = incoming_commits(remote, branch)
    if not incoming:
        print("Asha is already up to date.")
        return False
    print(f"[asha-update] {len(incoming)} incoming commit(s):")
    for line in incoming:
        print("  " + line)
    if dry_run:
        print("[asha-update] DRY-RUN: no mutation performed.")
        return False

    before = git("rev-parse", "HEAD").stdout.strip()
    proc = git("merge", "--ff-only", f"{remote}/{branch}")
    if proc.returncode != 0:
        raise UpdateError(
            "fast-forward merge refused (diverged history?): " + proc.stderr.strip()
        )
    print(f"[asha-update] merged {remote}/{branch} ({before} -> {git('rev-parse', 'HEAD').stdout.strip()})")

    try:
        audit_self(dry_run=False)
    except UpdateError as exc:
        print(f"[asha-update] GATE FAILURE: {exc}", file=sys.stderr)
        rollback()
        raise UpdateError("update rolled back; gates red") from exc
    print("[asha-update] gates green; self update complete")
    return True


def update_submodule(entry: dict, dry_run: bool) -> bool:
    name = entry.get("name")
    if not name:
        raise UpdateError("submodule entry needs name")
    path = ROOT / name
    if not (path / ".git").exists() and not (path / ".git").is_file():
        raise UpdateError(f"submodule {name} has no .git; ensure git submodule update --init first")
    remote = entry.get("remote", "origin")
    branch = entry.get("branch", "main")
    cmd = entry.get("test_command")

    print(f"[asha-update] submodule {name}: {remote}/{branch}")
    status = run(["git", "status", "--porcelain"], cwd=path)
    if status.stdout.strip():
        raise UpdateError(f"submodule {name} working tree dirty; refusing")
    run(["git", "fetch", remote, branch], cwd=path)
    incoming = run(["git", "log", "--oneline", f"HEAD..{remote}/{branch}"], cwd=path)
    if not incoming.stdout.strip():
        print(f"[asha-update] {name} already up to date.")
        return False
    if dry_run:
        print(f"[asha-update] DRY-RUN: {name} would update; no mutation performed.")
        return False
    before = run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()
    merged = run(["git", "merge", "--ff-only", f"{remote}/{branch}"], cwd=path)
    if merged.returncode != 0:
        raise UpdateError(f"{name} ff-only merge failed: {merged.stderr.strip()}")
    if cmd:
        proc = run(cmd, cwd=path)
        if proc.returncode != 0:
            run(["git", "reset", "--hard", "HEAD@{1}"], cwd=path)
            raise UpdateError(f"{name} audit failed (exit {proc.returncode}); rolled back")
    print(f"[asha-update] {name} updated {before} -> {run(['git', 'rev-parse', 'HEAD'], cwd=path).stdout.strip()}")
    return True


def main() -> int:
    global ROOT, DEPS, CODE_SEARCH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="inspect only; no mutation")
    parser.add_argument("--root", default=str(ROOT),
                        help="repo root override (tests use a scratch repo)")
    args = parser.parse_args()
    if args.root != str(ROOT):
        # tests: repoint the module globals at the scratch root
        ROOT = Path(args.root).resolve()
        DEPS = ROOT / ".jspace" / "dependencies.json"
        CODE_SEARCH = ROOT / "asha" / "code_search.py"

    changed = False
    try:
        deps = load_deps()
        changed |= update_self(args.dry_run)
        for entry in deps.get("submodules", []):
            changed |= update_submodule(entry, args.dry_run)
    except UpdateError as exc:
        print(f"[asha-update] REFUSED: {exc}", file=sys.stderr)
        return 1
    if not changed and not args.dry_run:
        print("[asha-update] nothing to update.")
    return 0


if __name__ == "__main__":
    sys.exit(main())