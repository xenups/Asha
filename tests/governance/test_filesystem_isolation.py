"""Filesystem isolation tests (Phase D).

Verify the target repository stays free of Asha-generated state:

* A. No .jspace created in the repo
* B. Repo file tree unchanged by Asha (only test-created files differ)
* C. git status --porcelain empty after Asha operations
* D. External state dir receives journal/evidence artifacts
* E. Evidence serialization compatibility (existing serializer)
* F. Journal compatibility (existing event/serialization code)

Uses ASHA_STATE_DIR set to a test-controlled tmp_path — never the
developer's real home state.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from asha import evidence, telemetry  # noqa: E402

TEST_REPO_NAME = "iso-repo"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


@pytest.fixture()
def iso_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Synthetic git repo + ASHA_STATE_DIR pinned to test tmp_path."""
    state = tmp_path / "asha-state"
    state.mkdir()
    monkeypatch.setenv("ASHA_STATE_DIR", str(state))

    root = tmp_path / TEST_REPO_NAME
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "tests")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "mod.py").write_text("VALUE: int = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _tree(root: Path) -> set[str]:
    """Relative file paths under the repo (including hidden dirs)."""
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if ".git" not in str(p.relative_to(root))
    }


class TestNoJspace:
    def test_no_jspace_created(self, iso_repo: Path) -> None:
        (iso_repo / "mod.py").write_text("VALUE: int = 2\n", encoding="utf-8")
        _git(iso_repo, "add", "-A")
        _git(iso_repo, "commit", "-qm", "change")

        # Run a real evidence seal + journal write (the canonical paths).
        sealed = evidence.seal({"commit": "deadbeef", "scope": "S0"})
        evidence.write(iso_repo, sealed)
        journal = telemetry.EventJournalWriter(iso_repo, "run-iso-1")
        journal.append("change_detected", {"target_files": ["mod.py"]})
        journal.close()

        assert not (iso_repo / ".jspace").exists()


class TestTreeUnchanged:
    def test_repo_tree_unchanged_by_asha(
        self, iso_repo: Path,
    ) -> None:
        before = _tree(iso_repo)
        # commit a change so the engine has something to evaluate
        (iso_repo / "mod.py").write_text("VALUE: int = 3\n", encoding="utf-8")
        _git(iso_repo, "add", "-A")
        _git(iso_repo, "commit", "-qm", "change2")
        before2 = _tree(iso_repo)  # test-created commit only

        sealed = evidence.seal({"commit": "abc", "scope": "S1"})
        evidence.write(iso_repo, sealed)
        journal = telemetry.EventJournalWriter(iso_repo, "run-iso-2")
        journal.append("scope_assessed", {"scope": "S1"})
        journal.close()

        after = _tree(iso_repo)
        assert before2 == after, (
            f"repo tree changed by Asha: {before2 ^ after}"
        )


class TestGitClean:
    def test_porcelain_empty_after_asha_ops(self, iso_repo: Path) -> None:
        (iso_repo / "mod.py").write_text("VALUE: int = 4\n", encoding="utf-8")
        _git(iso_repo, "add", "-A")
        _git(iso_repo, "commit", "-qm", "change3")

        sealed = evidence.seal({"commit": "def", "scope": "S2"})
        evidence.write(iso_repo, sealed)
        journal = telemetry.EventJournalWriter(iso_repo, "run-iso-3")
        journal.append("execution_started", {})
        journal.close()

        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=iso_repo,
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout
        assert out == "", f"Asha dirtied the repo: {out!r}"


class TestExternalState:
    def test_artifacts_under_state_dir(self, iso_repo: Path) -> None:
        (iso_repo / "mod.py").write_text("VALUE: int = 5\n", encoding="utf-8")
        _git(iso_repo, "add", "-A")
        _git(iso_repo, "commit", "-qm", "change4")

        sealed = evidence.seal({"commit": "1234", "scope": "S0"})
        evidence.write(iso_repo, sealed)
        journal = telemetry.EventJournalWriter(iso_repo, "run-iso-4")
        journal.append("sealed", {"evidence_id": "abc123"})
        journal.close()

        state = Path(iso_repo).parents[0] / "asha-state"
        ev_files = list(state.rglob("evidence*.json"))
        jr_files = list(state.rglob("run-iso-4.jsonl"))
        assert ev_files, f"no evidence under {state}"
        assert jr_files, f"no journal under {state}"
        # artifacts must NOT be under the repo
        assert not (iso_repo / "asha-state").exists()


class TestEvidenceCompatibility:
    def test_existing_serializer_still_used(self, iso_repo: Path) -> None:
        # The relocated write must produce the same bytes the original
        # writer produced: same json.dumps(indent=2, sort_keys=True) +
        # same digest computation. We verify by re-reading the file and
        # re-sealing: digest of the read payload equals recorded digest.
        sealed = evidence.seal({"commit": "beef", "scope": "S0"})
        path = evidence.write(iso_repo, sealed)
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
        assert payload["evidence_sha256"] == evidence.compute_digest(payload)
        # canonical serialization confirmed
        assert "\n  \"commit\":" in raw  # indent=2, sort_keys=True


class TestJournalCompatibility:
    def test_existing_event_construction_unchanged(
        self, iso_repo: Path,
    ) -> None:
        journal = telemetry.EventJournalWriter(iso_repo, "run-iso-5")
        event = journal.append("change_detected", {"target_files": ["a.py"]})
        journal.close()
        # event schema unchanged: five canonical fields
        assert set(event) == {
            "event_id", "run_id", "timestamp", "event_type", "payload",
        }
        raw = list(
            (Path(iso_repo).parents[0] / "asha-state")
            .rglob("run-iso-5.jsonl")
        )[0].read_text(encoding="utf-8")
        assert "change_detected" in raw
        assert '"target_files": ["a.py"]' in raw