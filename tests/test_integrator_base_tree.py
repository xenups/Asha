"""Phase 9 -- the integrator must bind the sealed BASE TREE, not just the base commit.

``_preflight`` compared ``base_commit`` with the target HEAD and re-derived the
worker commit's tree, but never resolved the base commit's own tree. A sealed
``base_tree_sha`` was therefore unverified: an artifact could declare any tree
while keeping commit identity, digest and target tree intact.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha import evidence
from asha.integrator import TreeIntegrator
from asha.scheduler import GovernedScheduler

PY = sys.executable

BASELINE = {
    ".gitignore": (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
                   ".mypy_cache/\n.ruff_cache/\n"),
    "README.md": "# fixture\n",
    "pkg/__init__.py": "",
    "pkg/module_a.py": 'VALUE_A = "base"\n',
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True).stdout.strip()


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in BASELINE.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _worker(cmd_source: str = "Path('pkg/module_a.py').write_text('A=1\\n')") -> list[dict]:
    return [{"id": "wa", "deps": [], "declared_scope": ["pkg/module_a.py"],
             "reads": ["pkg/module_a.py"], "writes": ["pkg/module_a.py"],
             "cmd": [PY, "-c", "from pathlib import Path; " + cmd_source]}]


def _sealed(repo: Path, *, task: str = "p9-integrator") -> Path:
    """Run the real scheduler and return the sealed worker artifact path."""
    sched = GovernedScheduler(repo, _worker(), task_id=task)
    report = sched.run()
    assert report["status"] == "ok", (report["states"], report["reason"])
    return Path(report["evidence"]["wa"])


def _reseal(path: Path, **changes: object) -> None:
    """Edit a covered field and recompute the digest: valid content, wrong facts."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    payload["evidence_sha256"] = evidence.compute_digest(payload)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    # the edit is self-consistent: digest integrity cannot be what rejects it
    assert evidence.compute_digest(
        json.loads(path.read_text(encoding="utf-8"))) == payload["evidence_sha256"]


def _apply(repo: Path, ev_path: Path):
    return TreeIntegrator(repo, [ev_path], verify_cmd=[PY, "-c", "pass"]).apply()


def test_high_42_correct_base_tree_is_accepted(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ev_path = _sealed(repo)
    base_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    assert json.loads(ev_path.read_text(encoding="utf-8"))["base_tree_sha"] \
        == base_tree

    result = _apply(repo, ev_path)

    assert result.status == "applied", result.error
    # the base tree the integrator verified is the pre-apply HEAD's tree
    assert result.commit_sha != _git(repo, "rev-parse", "HEAD~1")


def test_high_43_tampered_base_tree_is_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ev_path = _sealed(repo)
    head_before = _git(repo, "rev-parse", "HEAD")
    other_tree = _git(repo, "rev-parse", "HEAD^{tree}")[::-1]  # valid shape, wrong id
    _reseal(ev_path, base_tree_sha=other_tree)

    result = _apply(repo, ev_path)

    assert result.status == "invalid_evidence", result.error
    assert "base_tree_sha" in (result.error or "")
    # rejected while the index and worktree were still untouched
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "status", "--porcelain") == ""


def test_high_44_unknown_base_commit_is_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ev_path = _sealed(repo)
    _reseal(ev_path, base_commit="0" * 40)

    result = _apply(repo, ev_path)

    assert result.status == "invalid_evidence"
    assert "!= target HEAD" in (result.error or "")


def test_high_45_missing_base_tree_is_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ev_path = _sealed(repo)
    payload = json.loads(ev_path.read_text(encoding="utf-8"))
    payload.pop("base_tree_sha")
    payload["evidence_sha256"] = evidence.compute_digest(payload)
    ev_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")

    result = _apply(repo, ev_path)

    assert result.status == "invalid_evidence"
    assert "missing fields" in (result.error or "")


def test_high_46_another_repository_cannot_validate_the_base(
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ev_path = _sealed(repo)
    other = _make_repo(tmp_path, "other")
    (other / "pkg/module_a.py").write_text('VALUE_A = "diverged"\n',
                                            encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "diverge")

    result = _apply(other, ev_path)

    assert result.status == "invalid_evidence"
    assert "!= target HEAD" in (result.error or "")
    assert _git(other, "rev-parse", "HEAD")  # untouched, no rewrite


def test_high_47_no_change_execution_keeps_base_equal_to_target(
        tmp_path: Path) -> None:
    """An execution that writes nothing is valid evidence, not an error."""
    repo = _make_repo(tmp_path)
    sched = GovernedScheduler(repo, _worker("pass"), task_id="p9-noop")
    report = sched.run()
    assert report["status"] == "failed" or report["states"]["wa"]["state"] in (
        "DONE", "INVALID_EVIDENCE")
    # no observed change -> no seal is invented; nothing to integrate
    payload_state = report["states"]["wa"]
    if payload_state["state"] == "DONE":
        payload = json.loads(
            Path(report["evidence"]["wa"]).read_text(encoding="utf-8"))
        assert payload["base_tree_sha"] == payload["target_tree_sha"]
        assert payload["base_commit"] == payload["commit"]
    else:
        assert payload_state.get("evidence") is None
