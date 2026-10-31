"""Phase 8 -- worker-evidence identity binding across the real lifecycle.

Each production collector writes one sealed artifact per worker into
``<orchestrator>/<task>/<safe(worker_id)>.json``. The digest proves the bytes
were not edited after sealing; it does not prove WHICH worker the file belongs
to, so an artifact can be moved onto another worker's file name without
changing a byte. These tests exercise that on the real scheduler path.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha.governance.worker_execution import verify_worker_evidence
from asha.scheduler import GovernedScheduler
from asha.types import OrchestratorError

PY = sys.executable

BASELINE = {
    ".gitignore": (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
                   ".mypy_cache/\n.ruff_cache/\n"),
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    "pkg/module_a.py": 'VALUE_A = "original a"\n',
    "pkg/module_b.py": 'VALUE_B = "original b"\n',
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True).stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
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


def _pair() -> list[dict]:
    return [
        {"id": "wa", "deps": [], "declared_scope": ["pkg/module_a.py"],
         "reads": ["pkg/module_a.py"], "writes": ["pkg/module_a.py"],
         "cmd": [PY, "-c",
                 ("from pathlib import Path; "
                  "Path('pkg/module_a.py').write_text('VALUE_A = \"a2\"\\n')")]},
        {"id": "wb", "deps": [], "declared_scope": ["pkg/module_b.py"],
         "reads": ["pkg/module_b.py"], "writes": ["pkg/module_b.py"],
         "cmd": [PY, "-c",
                 ("from pathlib import Path; "
                  "Path('pkg/module_b.py').write_text('VALUE_B = \"b2\"\\n')")]},
    ]


@pytest.fixture()
def sealed_run(tmp_path: Path) -> dict:
    """One real scheduler run with two sealed worker artifacts."""
    repo = _make_repo(tmp_path)
    sched = GovernedScheduler(repo, _pair(), task_id="p8-identity")
    report = sched.run()
    assert report["status"] == "ok", (report["states"], report["reason"])
    assert set(report["evidence"]) == {"wa", "wb"}
    return report


def test_worker_evidence_is_bound_to_its_worker_identity(
        sealed_run: dict) -> None:
    """A byte-identical artifact must not pass as another worker's evidence.

    The digest covers content only, so without an explicit identity binding a
    perfectly sealed artifact can be moved onto another worker's file and every
    existing check (digest, tree re-bind, merge law) still passes.
    """
    wa_path = Path(sealed_run["evidence"]["wa"])
    wb_path = Path(sealed_run["evidence"]["wb"])
    assert verify_worker_evidence(
        wa_path, expected_worker_id="wa")["worker_id"] == "wa"

    wb_path.write_bytes(wa_path.read_bytes())      # byte-identical, wrong worker
    with pytest.raises(OrchestratorError, match="identity mismatch"):
        verify_worker_evidence(wb_path, expected_worker_id="wb")


def test_worker_evidence_identity_survives_a_process_restart(
        sealed_run: dict) -> None:
    """A fresh process re-reads the artifact and re-derives every identity."""
    path = Path(sealed_run["evidence"]["wa"])
    probe = (
        "import json, sys, hashlib\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from asha import evidence\n"
        "raw = Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "payload = json.loads(raw)\n"
        "print(json.dumps({\n"
        "    'worker_id': payload['worker_id'],\n"
        "    'task_id': payload['task_id'],\n"
        "    'tree_hash': payload['tree_hash'],\n"
        "    'target_tree_sha': payload['target_tree_sha'],\n"
        "    'base_tree_sha': payload['base_tree_sha'],\n"
        "    'authorized_to_ship': payload['authorized_to_ship'],\n"
        "    'digest_ok': evidence.compute_digest(payload)\n"
        "                 == payload['evidence_sha256'],\n"
        "    'on_disk_bytes_sha256':\n"
        "        hashlib.sha256(raw.encode('utf-8')).hexdigest(),\n"
        "}))\n"
    )
    out = subprocess.run([PY, "-c", probe, str(path), str(REPO_ROOT)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    fresh = json.loads(out.stdout)

    original = json.loads(path.read_text(encoding="utf-8"))
    assert fresh["worker_id"] == "wa"
    assert fresh["task_id"] == "p8-identity"
    assert fresh["digest_ok"] is True
    assert fresh["authorized_to_ship"] is False
    assert fresh["target_tree_sha"] == original["tree_hash"]
    assert fresh["tree_hash"] == original["tree_hash"]
    assert fresh["base_tree_sha"] == original["base_tree_sha"]
    assert fresh["base_tree_sha"] != fresh["target_tree_sha"]


def test_tampered_worker_evidence_fails_closed(sealed_run: dict) -> None:
    path = Path(sealed_run["evidence"]["wa"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["authorized_to_ship"] = True          # digest-covered field
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")

    with pytest.raises(OrchestratorError, match="digest mismatch"):
        verify_worker_evidence(path)


def test_unsigned_or_missing_digest_is_never_proof(sealed_run: dict) -> None:
    path = Path(sealed_run["evidence"]["wb"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("evidence_sha256")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")

    with pytest.raises(OrchestratorError):
        verify_worker_evidence(path)
    # a payload without a digest is not "self-consistent"; it is unsigned
    assert "evidence_sha256" not in payload


def _collect_failure(tmp_path: Path, exc: BaseException):
    repo = _make_repo(tmp_path)
    sched = GovernedScheduler(repo, _pair(), task_id="p8-collect-fail")
    real = sched._collect

    def flaky(worker: dict, path: Path, rc: int, tail: str, **kwargs: object):
        if str(worker["id"]) == "wa":
            raise exc
        return real(worker, path, rc, tail, **kwargs)  # type: ignore[arg-type]

    sched._collect = flaky                          # type: ignore[method-assign]
    report = sched.run()
    return report, sched


@pytest.mark.parametrize("exc", [RuntimeError("boom in collection"),
                                 OSError("disk gone")])
def test_collection_failure_fails_closed_without_partial_publication(
        tmp_path: Path, exc: BaseException) -> None:
    """A raising collector must not seal, publish, or hide the sibling."""
    report, sched = _collect_failure(tmp_path, exc)

    assert report["status"] == "failed"
    assert sched.state_of("wa") == "INVALID_EVIDENCE"
    assert report["states"]["wa"]["reason"] == f"internal: {exc}"
    # the failing worker carries no evidence pointer at all
    assert report["states"]["wa"].get("evidence") is None
    assert "wa" not in report["evidence"]
    # the sibling's own evidence is intact and is the only publication
    assert sorted(report["evidence"]) == ["wb"]
    assert sched.state_of("wb") == "DONE"
    assert verify_worker_evidence(
        Path(report["evidence"]["wb"]), expected_worker_id="wb")["worker_id"] \
        == "wb"
    assert sched.graph.generation == len(
        [entry for entry in sched.reconcile_log if entry["ok"]]) == 1
    # no artifact is invented for the worker whose collection failed
    run_dir = Path(report["evidence"]["wb"]).parent
    assert not (run_dir / "wa.json").exists()
