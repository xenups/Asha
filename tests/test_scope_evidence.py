"""End-to-end tests for scope resolution (S0-S4) and the evidence engine.

Every test builds a genuine scratch git repository (git init + baseline
commit + change commit) and drives either the scope resolver in-process or
the full `control.py check --stage ship` CLI, including clean-tree refusal
and evidence tamper detection.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL = REPO_ROOT / ".jspace" / "control.py"
PY = sys.executable

GITIGNORE = (
    ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
    ".mypy_cache/\n.ruff_cache/\n"
)

BASELINE = {
    ".gitignore": GITIGNORE,
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    "pkg/leaf.py": (
        "def _helper(x):\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def area(x):\n"
        "    return x * x\n"
    ),
}


def _load(name: str):
    spec = importlib.util.find_spec("asha." + name)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


scope_resolver = _load("scope_resolver")
evidence = _load("evidence")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=60, check=True)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in BASELINE.items():
        _write(repo, rel, text)
    _commit_all(repo, "baseline")
    return repo


def _resolve(repo: Path) -> dict:
    return scope_resolver.resolve(repo, base="HEAD~1")


def _control(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(CONTROL), "--transport", "local", "--root", str(repo), *args],
        cwd=repo, capture_output=True, text=True, timeout=300,
    )


def _ready_ledger(repo: Path) -> None:
    proc = _control(repo, "init", "--goal", "g", "--next", "n")
    assert proc.returncode == 0, proc.stderr
    proc = _control(repo, "read", "SKILL.md", "modules/self-monitoring.md")
    assert proc.returncode == 0, proc.stderr


# ---- STEP 0 contract -----------------------------------------------------

def test_control_questions_model() -> None:
    spec = importlib.util.spec_from_file_location("control_model", CONTROL)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.default_state(Path("."), "medium")["questions"] == {}
    state = module.default_state(Path("."), "medium")
    state["goal"] = "g"
    state["next"] = "n"
    state["transport"] = "local"
    state["questions"]["1"] = {"question": "q", "checkpoint": 1,
                               "closed": False}
    module.validate(state)
    legacy = dict(state)
    legacy["questions"] = [{"question": "q", "checkpoint": 1, "closed": False}]
    try:
        module.validate(legacy)
    except module.ControlError:
        pass
    else:
        raise AssertionError("legacy list shape must be rejected")


# ---- scope resolution ----------------------------------------------------

def test_s0_test_and_docs_only(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "README.md", "# fixture\n\nmore docs\n")
    _write(repo, "tests/test_extra.py", "def test_extra():\n    assert 1\n")
    _commit_all(repo, "docs and tests only")
    result = _resolve(repo)
    assert result["scope"] == "S0", result["reasons"]
    assert result["status"] == "certain"
    assert result["checks"] == ["pytest"]


def test_s1_private_leaf(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "pkg/leaf.py",
           "def _helper(x):\n    return x + 2\n\n\n"
           "def area(x):\n    return x * x\n")
    _commit_all(repo, "tweak private helper body")
    result = _resolve(repo)
    assert result["scope"] == "S1", result["reasons"]
    assert result["status"] == "certain"
    assert result["checks"] == ["pytest"], "S1 mandates direct tests only"


def test_s3_signature_contract_drift(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "pkg/leaf.py",
           "def _helper(x):\n    return x + 1\n\n\n"
           "def area(x, scale=1):\n    return x * x * scale\n")
    _commit_all(repo, "public signature drift")
    result = _resolve(repo)
    assert result["scope"] == "S3", result["reasons"]
    assert "mypy" in result["checks"], "S3 mandates downstream checks"


def test_s4_toolchain_hard_override(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # pyproject.toml touched AND a private helper changed: S4 must win.
    _write(repo, "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    _write(repo, "pkg/leaf.py",
           "def _helper(x):\n    return x + 3\n\n\n"
           "def area(x):\n    return x * x\n")
    _commit_all(repo, "toolchain override")
    result = _resolve(repo)
    assert result["scope"] == "S4", result["reasons"]
    assert result["per_file"]["pyproject.toml"] == "S4"
    assert result["per_file"]["pkg/leaf.py"] == "S1"


def test_scope_fail_closed_on_ambiguous_impact(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "pkg/dyn.py",
           "def _calc(x):\n    return x + 1\n\n\n"
           "def snapshot():\n    return dict(locals())\n")
    _commit_all(repo, "add dynamic module")
    _write(repo, "pkg/dyn.py",
           "def _calc(x):\n    return x + 99\n\n\n"
           "def snapshot():\n    return dict(locals())\n")
    _commit_all(repo, "edit helper next to dynamic lookup")
    result = _resolve(repo)
    assert result["scope"] in {"S3", "S4"}, result["reasons"]
    assert result["status"] == "uncertain", (
        "ambiguity must be flagged and never down-scoped")


def test_rename_and_deletion_handling(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "pkg/spare.py", "def spare():\n    return 0\n")
    _commit_all(repo, "add spare module")
    _git(repo, "mv", "pkg/leaf.py", "pkg/renamed_leaf.py")
    _git(repo, "rm", "-q", "pkg/spare.py")
    _commit_all(repo, "rename and delete")
    result = _resolve(repo)
    assert result["scope"] in scope_resolver.LEVELS, result["reasons"]
    assert result["scope"] == "S3", (
        "rename/delete of modules elevates: downstream imports unknown, "
        f"got {result['scope']}: {result['reasons']}")


# ---- evidence engine (full CLI) -----------------------------------------

def test_evidence_json_integrity(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _ready_ledger(repo)
    proc = _control(repo, "check", "--stage", "ship")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "GATE SHIP: PASS" in proc.stdout

    payload = json.loads(
        (repo / ".jspace" / "evidence.json").read_text(encoding="utf-8"))
    for key in ("schema", "stage", "scope", "commit", "tree_hash",
                "observed_at", "checks", "authorized_to_ship",
                "evidence_sha256"):
        assert key in payload, f"missing {key}"
    assert payload["schema"] == 1
    assert payload["stage"] == "ship"
    assert payload["scope"] in scope_resolver.LEVELS
    assert payload["authorized_to_ship"] is True
    assert payload["checks"], "at least one check must be recorded"
    for check in payload["checks"]:
        assert check["status"] in {"passed", "skipped"}
        assert check["exit_code"] == 0
    # canonical digest roundtrip
    recomputed = evidence.compute_digest(payload)
    assert recomputed == payload["evidence_sha256"]
    # exact tree binding
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True)
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo,
                          capture_output=True, text=True, check=True)
    assert payload["commit"] == head.stdout.strip()
    assert payload["tree_hash"] == tree.stdout.strip()


def test_ship_gate_refusal_on_failure(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _ready_ledger(repo)
    _write(repo, "bad.py", "import os\n")  # ruff F401
    _commit_all(repo, "introduce lint failure")
    proc = _control(repo, "check", "--stage", "ship")
    assert proc.returncode == 1, "failed check must refuse ship"
    assert "GATE SHIP: FAIL" in proc.stderr

    payload = json.loads(
        (repo / ".jspace" / "evidence.json").read_text(encoding="utf-8"))
    assert payload["authorized_to_ship"] is False
    failed = [c for c in payload["checks"] if c["status"] == "failed"]
    assert failed and failed[0]["name"] == "ruff"
    assert failed[0]["exit_code"] != 0
    # artifact still sealed correctly
    assert evidence.compute_digest(payload) == payload["evidence_sha256"]


def test_evidence_binds_to_exact_tree(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _ready_ledger(repo)
    proc = _control(repo, "check", "--stage", "ship")
    assert proc.returncode == 0, proc.stderr
    artifact = repo / ".jspace" / "evidence.json"
    before = artifact.read_bytes()

    # Dirty a tracked file WITHOUT committing: tree mismatch -> refuse.
    _write(repo, "tests/test_ok.py",
           "def test_ok():\n    assert False  # dirty\n")
    proc = _control(repo, "check", "--stage", "ship")
    assert proc.returncode == 1, "dirty tree must fail closed"
    assert "SHIP GATE REFUSED" in proc.stderr
    assert "dirty" in proc.stderr
    assert artifact.read_bytes() == before, (
        "a refused run must never overwrite sealed evidence")


def test_evidence_tamper_detection(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _ready_ledger(repo)
    _write(repo, "bad.py", "import os\n")  # failing run -> authorized false
    _commit_all(repo, "failing baseline")
    proc = _control(repo, "check", "--stage", "ship")
    assert proc.returncode == 1
    artifact = repo / ".jspace" / "evidence.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["authorized_to_ship"] is False

    # Manual tamper: flip authorization without resealing the digest.
    payload["authorized_to_ship"] = True
    artifact.write_text(json.dumps(payload, indent=2, sort_keys=True),
                        encoding="utf-8")
    proc = _control(repo, "check", "--stage", "ship")
    assert proc.returncode == 1, "tampered evidence must be rejected"
    assert "SHIP GATE REFUSED" in proc.stderr
    assert "evidence_sha256 mismatch" in proc.stderr
