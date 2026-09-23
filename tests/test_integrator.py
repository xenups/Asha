"""Phase 4 TDD: governed tree integration & atomic `--apply`.

Pre-implementation failure demonstrated: `orchestrator.TreeIntegrator` /
`IntegrationResult` do not exist yet (AttributeError) and the CLI does not
know `--apply` (argparse exit 2). After implementation: dry-run must leave
the target untouched, apply must be ONE atomic commit, and the golden
invariant PASS(A)+PASS(B) != PASS(A U B) must fail-closed with a complete
rollback (as must tampered evidence and merge collisions).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTROL = REPO / ".jspace" / "control.py"
TOOLS = REPO / ".hermes" / "tools"

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import evidence  # (TOOLS must be on sys.path before these imports)
import orchestrator

PY = sys.executable


# -- fixtures (house style: real scratch git repo) ---------------------------

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True)
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(
        ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n")
    (repo / "README.md").write_text("# p4 fixture\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _write_cmd(target: str, body: str = "import os\n") -> list[str]:
    script = ("from pathlib import Path\n"
              f"Path({target!r}).write_text({body!r})\n")
    return [PY, "-c", script]


def _worker(wid: str, deps: list[str] | None = None,
            writes: list[str] | None = None,
            cmd: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": wid,
        "deps": deps or [],
        "declared_scope": ["tests/"],
        "reads": [],
        "writes": writes or [],
        "cmd": [PY, "-c", "pass"] if cmd is None else cmd,
    }


def _spec(tmp_path: Path, task_id: str, workers: list[dict[str, Any]]
          ) -> Path:
    path = tmp_path / f"spec-{task_id}.json"
    path.write_text(json.dumps({"task_id": task_id, "workers": workers}),
                    encoding="utf-8")
    return path


def _control(repo: Path, spec: Path,
             apply: bool = False) -> subprocess.CompletedProcess[str]:
    argv = [PY, str(CONTROL), "--transport", "local",
            "--root", str(repo), "orchestrator", "--spec", str(spec)]
    if apply:
        argv.append("--apply")
    return subprocess.run(argv, cwd=repo, capture_output=True, text=True,
                          timeout=600)


def _worktrees_dir(repo: Path) -> Path:
    return repo.parent / (repo.name + ".worktrees")


# -- 1. Dry-run default: --apply absent leaves the target untouched ----------

def test_dry_run_default_leaves_target_untouched(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head0 = _git(repo, "rev-parse", "HEAD")
    spec = _spec(tmp_path, "dry", [
        _worker("A", writes=["tests/a.py"], cmd=_write_cmd("tests/a.py"))])

    proc = _control(repo, spec)  # NO --apply

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["status"] == "ok"
    assert report["states"]["A"]["state"] == "DONE"
    # audit/inspection run: no integration stage at all
    assert "integration" not in report
    assert _git(repo, "rev-parse", "HEAD") == head0
    assert _git(repo, "status", "--porcelain") == ""
    assert not _worktrees_dir(repo).exists()


# -- 2. Successful atomic apply: one commit carrying both workers -------------

def test_apply_creates_single_atomic_commit(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head0 = _git(repo, "rev-parse", "HEAD")
    spec = _spec(tmp_path, "atomic", [
        _worker("A", writes=["tests/a.py"], cmd=_write_cmd("tests/a.py")),
        _worker("B", writes=["tests/b.py"],
                cmd=_write_cmd("tests/b.py", "import pathlib\n")),
    ])

    proc = _control(repo, spec, apply=True)

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["status"] == "ok"
    integ = report["integration"]
    assert integ["status"] == "applied"
    assert integ["commit_sha"] == _git(repo, "rev-parse", "HEAD")
    assert sorted(integ["workers"]) == ["A", "B"]
    assert len(integ["evidence_shas"]) == 2
    assert integ["generation"] == report["graph"]["generation"]
    # exactly ONE new commit on the target branch, carrying both files
    assert _git(repo, "rev-list", "--count", f"{head0}..HEAD") == "1"
    files = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert {"tests/a.py", "tests/b.py"} <= files
    assert _git(repo, "status", "--porcelain") == ""
    assert not _worktrees_dir(repo).exists()


# -- 3. Golden invariant: PASS(A)+PASS(B) != PASS(A U B) ----------------------

def test_integration_failure_rolls_back_completely(tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    repo = _make_repo(tmp_path)
    head0 = _git(repo, "rev-parse", "HEAD")
    # A's test passes alone (no marker in ITS worktree) and B's change
    # passes alone -- but their union breaks A's test. Disjoint paths, so
    # the cherry-pick itself never conflicts: only the INTEGRATION gate
    # can catch it, which is exactly the golden invariant.
    marker_test = (
        "from pathlib import Path\n"
        "def test_marker_absent():\n"
        "    assert not (Path(__file__).parent / 'b_marker.txt').exists()\n"
    )
    spec = _spec(tmp_path, "union", [
        _worker("A", writes=["tests/test_a.py"],
                cmd=_write_cmd("tests/test_a.py", marker_test)),
        _worker("B", writes=["tests/b_marker.txt"],
                cmd=_write_cmd("tests/b_marker.txt", "present\n")),
    ])

    rc = orchestrator.main(["--root", str(repo), "run", "--spec", str(spec),
                            "--apply"])
    report = json.loads(capsys.readouterr().out)

    assert rc != 0
    assert report["status"] == "ok"  # individually every worker passed
    integ = report["integration"]
    assert integ["status"] == "verification_failed"
    assert "test_a" in (integ["error"] or "")  # the failing half is named
    # complete rollback to the pre-apply state, zero debris
    assert _git(repo, "rev-parse", "HEAD") == head0
    assert _git(repo, "status", "--porcelain") == ""
    assert not (repo / "tests" / "test_a.py").exists()
    assert not (repo / "tests" / "b_marker.txt").exists()


# -- 4. Tampered/missing evidence rejected BEFORE any file changes ------------

def test_invalid_evidence_aborts_before_any_mutation(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    head0 = _git(repo, "rev-parse", "HEAD")
    base_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    # a real dangling commit standing in for a worker result tree
    (repo / "tests" / "shared_payload.py").write_text("PAYLOAD = 1\n",
                                                      encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=f@x.invalid",
         "commit", "-qm", "worker-ish")
    target_commit = _git(repo, "rev-parse", "HEAD")
    target_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "reset", "--hard", head0)  # target branch stays at head0
    assert _git(repo, "status", "--porcelain") == ""

    def sealed(worker_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": evidence.SCHEMA, "stage": "worker",
            "scope": "tests", "commit": target_commit,
            "tree_hash": target_tree, "observed_at": evidence.now_iso(),
            "checks": [{"name": "pytest", "status": "passed",
                        "exit_code": 0}],
            "authorized_to_ship": False, "task_id": "p4",
            "worker_id": worker_id, "base_commit": head0,
            "base_tree_sha": base_tree, "target_tree_sha": target_tree,
            "declared_scope": ["tests/"],
            "observed_scope": ["tests/shared_payload.py"],
            "read_set": [], "write_set": ["tests/shared_payload.py"],
            "diff": "", "exit_status": 0,
        }
        return evidence.seal(payload)

    good = tmp_path / "evi-good.json"
    good.write_text(json.dumps(sealed("A")), encoding="utf-8")
    tampered = tmp_path / "evi-tampered.json"
    raw = json.dumps(sealed("B"))
    tampered.write_text(raw.replace('"worker_id": "B"',
                                    '"worker_id": "MALLORY"'),
                        encoding="utf-8")
    missing = tmp_path / "evi-missing.json"

    for label, paths in (("tampered", [good, tampered]),
                         ("missing", [good, missing])):
        integrator = orchestrator.TreeIntegrator(repo, paths, generation=3)
        result = integrator.apply()
        assert result.status == "invalid_evidence", (label, result)
        assert result.error
        # rejected before anything was staged or written
        assert _git(repo, "rev-parse", "HEAD") == head0
        assert _git(repo, "status", "--porcelain") == ""


# -- 5. Merge collision during apply -> clean abort, zero debris ---------------

def test_merge_collision_aborts_cleanly(tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    repo = _make_repo(tmp_path)
    head0 = _git(repo, "rev-parse", "HEAD")
    # A and B rewrite the SAME line of the SAME file in separate
    # worktrees (B defers until A finishes, so both complete DONE) --
    # their cherry-picks onto the target branch must collide.
    spec = _spec(tmp_path, "clash", [
        _worker("A", writes=["tests/shared.txt"],
                cmd=_write_cmd("tests/shared.txt", "A-version\n")),
        _worker("B", writes=["tests/shared.txt"],
                cmd=_write_cmd("tests/shared.txt", "B-version\n")),
    ])

    rc = orchestrator.main(["--root", str(repo), "run", "--spec", str(spec),
                            "--apply"])
    report = json.loads(capsys.readouterr().out)

    assert rc != 0
    assert report["status"] == "ok"  # both workers individually DONE
    integ = report["integration"]
    assert integ["status"] == "conflict"
    assert "conflict" in (integ["error"] or "").lower()
    # clean abort: original HEAD, no leftover half-applied files, no debris
    assert _git(repo, "rev-parse", "HEAD") == head0
    assert _git(repo, "status", "--porcelain") == ""
    seq = subprocess.run(["git", "rev-parse", "--verify", "-q",
                         "CHERRY_PICK_HEAD"], cwd=repo,
                        capture_output=True)
    assert seq.returncode != 0  # cherry-pick sequence state cleared
    assert not _worktrees_dir(repo).exists()
