"""Regression tests for scripts/update.py (atomic fail-closed self-update)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE = REPO_ROOT / "scripts" / "update.py"
PY = sys.executable

# Tests run against a scratch git repo so the real harness tree is never
# touched: same protocol steps, isolated fixtures.
def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c1"], cwd=repo, check=True)
    return repo


def _write_deps(dest: Path) -> None:
    dest.parent.mkdir(exist_ok=True)
    dest.write_text('{"self": {"remote": "origin", "branch": "main", "pinned": true}, "submodules": []}',
                    encoding="utf-8")


def _commit_deps(repo: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add deps"], cwd=repo, check=True)


def _run_update(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(UPDATE), "--root", str(cwd), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_dry_run_up_to_date(tmp_path: Path) -> None:
    """No remote changes -> already up to date, exit 0, tree untouched."""
    repo = _make_repo(tmp_path, "r1")
    _write_deps(repo / ".jspace" / "dependencies.json")
    _commit_deps(repo)
    upstream_bare = tmp_path / "r1-upstream.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(upstream_bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(upstream_bare)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True)
    proc = _run_update(repo, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "already up to date" in proc.stdout
    assert (repo / "f.txt").read_text(encoding="utf-8") == "one\n"


def test_dirty_tree_refused(tmp_path: Path) -> None:
    """Fail-closed: dirty working tree must refuse before any fetch/merge."""
    repo = _make_repo(tmp_path, "r2")
    _write_deps(repo / ".jspace" / "dependencies.json")
    _commit_deps(repo)
    (repo / "f.txt").write_text("dirty\n", encoding="utf-8")
    proc = _run_update(repo, "--dry-run")
    assert proc.returncode == 1, proc.stdout
    assert "REFUSED" in proc.stderr
    assert "dirty" in proc.stderr


def test_missing_deps_config_refused(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "r3")
    proc = _run_update(repo, "--dry-run")
    assert proc.returncode == 1
    assert "dependencies.json" in proc.stderr


def test_dry_run_reports_incoming_without_mutating(tmp_path: Path) -> None:
    """Remote has new commits; --dry-run lists them but does not merge."""
    local = _make_repo(tmp_path, "r4")
    # clone as the fetchable remote (bare)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(local), str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=local, check=True)
    # advance the bare remote on its own checkout
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    (work / "f.txt").write_text("one\nnewline\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c2"], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True)

    _write_deps(local / ".jspace" / "dependencies.json")
    _commit_deps(local)
    proc = _run_update(local, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "DRY-RUN" in proc.stdout
    assert "incoming commit" in proc.stdout
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=local, capture_output=True, text=True)
    assert head.stdout.strip().startswith("0" * 6) is False  # anything valid
    # tree must NOT have advanced: still 2 commits (c1 + deps), no merge
    count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=local, capture_output=True, text=True)
    assert count.stdout.strip() == "2", count.stdout