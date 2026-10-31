"""Phase 1 -- audit snapshot integrity and fail-closed authorization.

Verification matrix from the phase spec (Stage 5), each case named for the
property it proves:

   1 determinism            identical manifest inputs -> identical snapshot_id
   2 content sensitivity    one changed byte -> different snapshot_id
   3 path sensitivity       move/rename -> different snapshot_id
   4 untracked coverage     eligible new files included
   5 deletion coverage      deleted tracked files represented unambiguously
   6 worktree/index         staged vs unstaged handled per documented policy
   7 unreadable input       inability to read a required file fails closed
   8 invalid evidence       empty/malformed/missing/unknown/inconsistent denied
   9 execution failure      crashed/timeout/non-success outcomes deny
  10 incomplete checks      omitted/skipped/unjustified-skip denies
  11 stale snapshot         post-audit modification -> STALE_REVIEW + denial
  12 frozen execution       checks that mutate the target are detected+denied
  13 single auth path       only the canonical gate can grant authorization
  14 positive control       a complete, valid, clean audit still passes

Every test that exercises the authorization BOUNDARY drives
asha.governance.ship_gate.gate_ship against a real scratch git repository
(never a bare helper return value). Pure contract cases drive
asha.gate_states directly, which IS the boundary: gate_ship derives its
verdict from exactly that function, so the mapping gate->verdict is shared
code, and the end-to-end cases below prove the wiring.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from asha import audit_snapshot, evidence, gate_states
from asha.common import paths as common_paths
from asha.governance import ship_gate

GITIGNORE = (
    ".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
    ".mypy_cache/\n.ruff_cache/\nvenv/\n"
)

BASELINE = {
    ".gitignore": GITIGNORE,
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    "pkg/leaf.py": "VALUE = 1\n",
}


# --------------------------------------------------------------- fixtures

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True,
                   text=True, timeout=60, check=True)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A real scratch repository with the fixture baseline committed."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in BASELINE.items():
        _write(repo, rel, text)
    _commit_all(repo, "baseline")
    return repo


def _evidence_file(repo: Path) -> Path:
    return common_paths.get_evidence_dir(repo) / "evidence.json"


def _sealed(repo: Path) -> dict:
    return json.loads(_evidence_file(repo).read_text(encoding="utf-8"))


def _contract(snapshot_id: str = "a" * 64,
              required: tuple[str, ...] = ("ruff", "pytest", "mypy"),
              checks: tuple[tuple[str, str, str], ...] = (
                  ("ruff", "passed", ""),
                  ("pytest", "passed", ""),
                  ("mypy", "passed", ""),
              ),
              findings: tuple[dict, ...] = (),
              policy_version: str | None = None,
              ) -> gate_states.GateEvidence:
    """Build a valid evidence contract; callers vary one field at a time.

    The third element of each ``checks`` row is the ``note`` (kept as the
    historical call shape); a ``CheckResult`` built from it gets the exit
    code implied by its status -- 0 for ``passed``/``skipped``, None for
    ``failed`` (the scoped-timeout shape) -- matching what
    ``check_runner`` produces. Rows that need non-default exit codes (the
    F4 adversarial cases) call ``gate_states.CheckResult`` directly.

    Phase 2: the default policy (ship_gate.POLICY_VERSION) is 'phase2',
    so a valid semantic-review artifact bound to this snapshot_id is
    synthesized automatically; pass ``policy_version="phase1"`` to build
    legacy-shaped evidence (findings allowed without an artifact).
    """
    policy = (policy_version if policy_version is not None
              else ship_gate.POLICY_VERSION)
    artifact = {
        "schema": "semantic-review/v1",
        "snapshot_id": snapshot_id,
        "policy_version": policy,
        "status": "ok",
        "findings": list(findings),
    }
    return gate_states.GateEvidence(
        snapshot_id=snapshot_id,
        evidence_schema=evidence.SCHEMA,
        policy_version=policy,
        required_checks=required,
        checks=tuple(
            gate_states.CheckResult(
                name=name, status=status, note=note,
                exit_code=0 if status in ("passed", "skipped") else None)
            for name, status, note in checks),
        findings=findings,
        semantic_review=artifact,
    )


def _finding(severity: str = "MEDIUM") -> dict:
    """A schema-valid reviewer finding; callers vary only the severity."""
    return {
        "severity": severity,
        "file_path": "pkg/leaf.py",
        "line_range": "1-2",
        "problem": "test problem",
        "why_it_matters": "test consequence",
        "failure_scenario": "test scenario",
        "recommended_fix": "test fix",
    }


def _clean_reviewer(root: Path, snapshot_id: str, timeout_s: int) -> dict:
    """Stub semantic reviewer: a clean, identity-bound review."""
    return {
        "schema": "semantic-review/v1",
        "snapshot_id": snapshot_id,
        "policy_version": ship_gate.POLICY_VERSION,
        "status": "ok",
        "findings": [],
    }


# ------------------------------------------------ 1-6 snapshot integrity

def test_1_determinism_identical_inputs_identical_id(tmp_path: Path) -> None:
    """Case 1: repeated builds of identical inputs agree, independent of
    filesystem enumeration order and of the repository location."""
    first = _make_repo(tmp_path, "one")
    second = _make_repo(tmp_path, "two")
    a = audit_snapshot.build_manifest(first).snapshot_id
    b = audit_snapshot.build_manifest(first).snapshot_id
    c = audit_snapshot.build_manifest(second).snapshot_id
    assert a == b, "two builds of the same tree must agree"
    assert a == c, "two clones with identical contents must agree"
    assert len(a) == 64


def test_2_content_sensitivity_one_byte_changes_id(tmp_path: Path) -> None:
    """Case 2: a single byte of difference must change the identifier."""
    repo = _make_repo(tmp_path)
    before = audit_snapshot.snapshot_id_of(repo)
    _write(repo, "pkg/leaf.py", "VALUE = 2\n")
    after = audit_snapshot.snapshot_id_of(repo)
    assert before != after, "one changed byte must change snapshot_id"
    entry = audit_snapshot.build_manifest(repo).index()["pkg/leaf.py"]
    assert entry.entry_type == audit_snapshot.ENTRY_FILE
    # the digest is over the BYTES ON DISK (the platform may expand \n)
    on_disk = (repo / "pkg/leaf.py").read_bytes()
    import hashlib
    assert entry.size == len(on_disk)
    assert entry.digest == hashlib.sha256(on_disk).hexdigest()


def test_3_path_sensitivity_rename_changes_id(tmp_path: Path) -> None:
    """Case 3: identical bytes at a different path must change the id."""
    repo = _make_repo(tmp_path)
    before = audit_snapshot.snapshot_id_of(repo)
    _git(repo, "mv", "pkg/leaf.py", "pkg/moved.py")
    after = audit_snapshot.snapshot_id_of(repo)
    assert before != after, "a moved file must change snapshot_id"
    paths = audit_snapshot.build_manifest(repo).paths
    assert "pkg/moved.py" in paths
    assert "pkg/leaf.py" not in paths


def test_4_untracked_files_are_covered(tmp_path: Path) -> None:
    """Case 4: an eligible new (untracked) file is part of the audit."""
    repo = _make_repo(tmp_path)
    before = audit_snapshot.snapshot_id_of(repo)
    _write(repo, "pkg/new_module.py", "NEW = True\n")
    manifest = audit_snapshot.build_manifest(repo)
    assert "pkg/new_module.py" in manifest.paths
    assert manifest.snapshot_id != before


def test_5_deletion_is_represented_unambiguously(tmp_path: Path) -> None:
    """Case 5: a deleted tracked file is an explicit deletion entry, and
    deleting it changes the identifier."""
    repo = _make_repo(tmp_path)
    before = audit_snapshot.snapshot_id_of(repo)
    (repo / "pkg" / "leaf.py").unlink()
    manifest = audit_snapshot.build_manifest(repo)
    entry = manifest.index()["pkg/leaf.py"]
    assert entry.entry_type == audit_snapshot.ENTRY_DELETED
    assert entry.digest == "" and entry.size == 0
    assert manifest.snapshot_id != before, (
        "a deletion must not be indistinguishable from an absent file")


def test_6_staged_and_unstaged_content_are_covered(tmp_path: Path) -> None:
    """Case 6: documented policy is working-tree bytes win; a staged-only
    change is still covered, and each state yields a distinct id."""
    repo = _make_repo(tmp_path)
    clean = audit_snapshot.snapshot_id_of(repo)
    _write(repo, "README.md", "# staged-only\n")
    _git(repo, "add", "README.md")
    staged = audit_snapshot.snapshot_id_of(repo)
    assert staged != clean, "a staged-only change must be covered"
    _write(repo, "README.md", "# worktree-wins\n")
    both = audit_snapshot.build_manifest(repo)
    assert both.snapshot_id != staged, (
        "unstaged bytes on top of staged content must change the id")
    on_disk = (repo / "README.md").read_bytes()
    import hashlib
    assert both.index()["README.md"].digest == hashlib.sha256(on_disk).hexdigest(), (
        "the audited digest must be the working-tree bytes")
    assert on_disk != b"# staged-only\n", "sanity: the two states differ"


def test_6b_exclusion_policy_is_deterministic(tmp_path: Path) -> None:
    """Supporting case 6: virtualenvs, caches and VCS state are excluded by
    the documented rules; the .gitignore itself is a required input."""
    repo = _make_repo(tmp_path)
    (repo / "venv").mkdir()
    (repo / "venv" / "junk.py").write_text("junk\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    paths = audit_snapshot.build_manifest(repo).paths
    assert not any(p.startswith("venv/") for p in paths)
    assert not any(p.startswith("__pycache__/") for p in paths)
    assert ".gitignore" in paths, "a required input must not be silently dropped"


# ---------------------------------------------------- 7 fail-closed input

def test_7_unreadable_required_file_fails_closed(tmp_path: Path,
                                                 monkeypatch) -> None:
    """Case 7: inability to read a required file must raise, never be
    silently skipped. A raising stub is used so the assertion cannot be
    vacuous (an uninstalled counter would pass regardless)."""
    repo = _make_repo(tmp_path)
    target = (repo / "pkg" / "leaf.py").resolve()
    real = Path.read_bytes

    def raiser(self: Path) -> bytes:
        if self.resolve() == target:
            raise OSError("simulated unreadable file")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", raiser)
    with pytest.raises(audit_snapshot.SnapshotError) as exc:
        audit_snapshot.build_manifest(repo)
    assert "unreadable" in str(exc.value)


def test_7b_symlink_entries_are_rejected(tmp_path: Path) -> None:
    """Case 7: an unsupported entry type (symlink) fails closed. Skipped
    only when the platform forbids creating the fixture."""
    repo = _make_repo(tmp_path)
    link = repo / "linked.py"
    try:
        link.symlink_to(repo / "README.md")
    except (OSError, NotImplementedError):
        pytest.skip("platform cannot create symlinks")
    with pytest.raises(audit_snapshot.SnapshotError) as exc:
        audit_snapshot.build_manifest(repo)
    assert "symlink" in str(exc.value)


def test_7c_non_repository_fails_closed(tmp_path: Path) -> None:
    """Case 7: no repository means no snapshot, never an empty one."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(audit_snapshot.SnapshotError):
        audit_snapshot.build_manifest(plain)


# -------------------------------------------------- 8 invalid evidence

@pytest.mark.parametrize("payload", [
    None,
    [],
    {},
    {"snapshot_id": "x"},
    {"snapshot_id": "x", "evidence_schema": 1, "policy_version": "p",
     "required_checks": [], "checks": []},
    {"snapshot_id": "x", "evidence_schema": 1, "policy_version": "p",
     "required_checks": ["ruff"], "checks": "not-a-list"},
    {"snapshot_id": "x", "evidence_schema": 1, "policy_version": "p",
     "required_checks": ["ruff"], "checks": [{"name": "ruff"}]},
    {"snapshot_id": "x", "evidence_schema": 1, "policy_version": "p",
     "required_checks": ["ruff"],
     "checks": [{"name": "ruff", "status": "totally-unknown"}]},
    {"snapshot_id": "", "evidence_schema": 1, "policy_version": "p",
     "required_checks": ["ruff"],
     "checks": [{"name": "ruff", "status": "passed"}]},
], ids=["none", "list", "empty-dict", "missing-fields", "empty-inventory",
        "checks-not-list", "check-without-status", "unknown-status",
        "blank-snapshot"])
def test_8_invalid_evidence_is_rejected(payload: object) -> None:
    """Case 8: empty, malformed, missing-field, unknown-state and
    empty-inventory evidence never validates."""
    with pytest.raises(gate_states.GateContractError):
        gate_states.validate_evidence(payload)


def test_8b_skipped_and_unexpected_inventories_deny() -> None:
    """Case 8: an inconsistent (incomplete or over-complete) check
    inventory is INVALID_EVIDENCE and never authorizes."""
    partial = _contract(checks=(("ruff", "passed", ""),
                                ("pytest", "passed", "")))
    decision = gate_states.evaluate(partial, authorized_snapshot_id="a" * 64)
    assert decision.state is gate_states.GateState.INVALID_EVIDENCE
    assert decision.authorized_to_ship is False
    assert "mypy" in decision.reasons[0]

    extra = _contract(checks=(("ruff", "passed", ""),
                              ("pytest", "passed", ""),
                              ("mypy", "passed", ""),
                              ("extra", "passed", "")))
    decision = gate_states.evaluate(extra, authorized_snapshot_id="a" * 64)
    assert decision.state is gate_states.GateState.INVALID_EVIDENCE
    assert decision.authorized_to_ship is False

    # a duplicated required check is rejected AT VALIDATION (not only at
    # evaluate), so the construction call is the one under test
    with pytest.raises(gate_states.GateContractError):
        gate_states.validate_evidence({
            "snapshot_id": "x" * 64, "evidence_schema": 1,
            "policy_version": "p",
            "required_checks": ["ruff", "ruff"],
            "checks": [{"name": "ruff", "status": "passed"}],
        })

    duplicate_runs = (("ruff", "passed", ""), ("ruff", "passed", ""),
                      ("pytest", "passed", ""))
    with pytest.raises(gate_states.GateContractError):
        gate_states.evaluate(_contract(checks=duplicate_runs), authorized_snapshot_id="a" * 64)


# ------------------------------------------------ 9 execution failures

@pytest.mark.parametrize("status", ["failed"])
def test_9_failing_check_denies(status: str) -> None:
    """Case 9: a failed check denies with EXECUTION_FAILED."""
    decision = gate_states.evaluate(_contract(checks=(
        ("ruff", status, ""), ("pytest", "passed", ""),
        ("mypy", "passed", ""))), authorized_snapshot_id="a" * 64)
    assert decision.state is gate_states.GateState.EXECUTION_FAILED
    assert decision.authorized_to_ship is False
    assert "ruff" in decision.reasons[0]


def test_9b_gate_denies_on_execution_failure_end_to_end(
        tmp_path: Path, monkeypatch) -> None:
    """Case 9: through the real gate -- a check that crashes/times out is
    sealed unauthorized and raises, with the state recorded in evidence."""
    repo = _make_repo(tmp_path)

    def fake_run(root: Path, resolved: dict,
             **_: object) -> list[dict]:
        return [
            {"name": "ruff", "status": "failed", "exit_code": 1,
             "note": "", "output_tail": "simulated crash"},
            {"name": "pytest", "status": "passed", "exit_code": 0, "note": ""},
            {"name": "mypy", "status": "passed", "exit_code": 0, "note": ""},
        ]

    monkeypatch.setattr(ship_gate.check_runner, "run", fake_run)
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo, reviewer=_clean_reviewer)
    assert "EXECUTION_FAILED" in str(exc.value)
    sealed = _sealed(repo)
    assert sealed["authorized_to_ship"] is False
    assert sealed["gate_state"] == "EXECUTION_FAILED"
    assert evidence.compute_digest(sealed) == sealed["evidence_sha256"]


def test_9c_gate_refuses_when_nothing_executed(tmp_path: Path) -> None:
    """Case 9/10: the historical fail-open -- an empty check inventory
    (all([]) is True) -- must now deny."""
    repo = _make_repo(tmp_path)
    with pytest.raises(evidence.EvidenceError):
        ship_gate.gate_ship(repo, no_execute=True)
    sealed = _sealed(repo)
    assert sealed["authorized_to_ship"] is False
    assert sealed["checks"] == []
    assert sealed["gate_state"] == "INVALID_EVIDENCE"


# ------------------------------------------------- 10 incomplete checks

def test_10_omitted_check_denies_and_justified_skip_does_not() -> None:
    """Case 10: an omitted mandatory check denies; a skip the runner
    PROVED (empty target set) keeps the existing non-failing contract."""

    # F3: the authorization identity is mandatory -- omitting it fail-closes.
    missing = gate_states.evaluate(
        _contract(), authorized_snapshot_id="")
    assert missing.state is gate_states.GateState.INVALID_EVIDENCE
    assert missing.authorized_to_ship is False

    omitted = gate_states.evaluate(_contract(
        required=("ruff", "pytest", "mypy", "coverage"),
        checks=(("ruff", "passed", ""), ("pytest", "passed", ""),
                ("mypy", "passed", ""))), authorized_snapshot_id="a" * 64)
    assert omitted.state is gate_states.GateState.INVALID_EVIDENCE
    assert omitted.authorized_to_ship is False

    # F1/F4: a skipped check's legitimacy is the status itself (the
    # runner's inapplicability path); the note is informational. A
    # hand-built entry with a made-up note and no recorded exit code
    # still carries the runner's shapes, so it remains a proven skip.
    # The all-skipped denial below is the real F1 guard.
    proven = gate_states.evaluate(_contract(checks=(
        ("ruff", "passed", ""), ("pytest", "passed", ""),
        ("mypy", "skipped", "no applicable target"))), authorized_snapshot_id="a" * 64)
    assert proven.state is gate_states.GateState.COMPLETED_CLEAN
    assert proven.authorized_to_ship is True

    # F1: the note string carries NO authority. A forged note on an
    # otherwise clean run changes nothing (already proven above), and a
    # skip is legitimate ONLY through the structural signal.
    clean = gate_states.evaluate(_contract(checks=(
        ("ruff", "passed", ""), ("pytest", "passed", ""),
        ("mypy", "passed", ""))), authorized_snapshot_id="a" * 64)
    assert clean.authorized_to_ship is True

    # A run in which NOTHING executed -- every check skipped -- is the
    # historical all([]) fail-open, reached through the skip path. No
    # policy permits an all-skipped audit to authorize shipping.
    all_skipped = gate_states.evaluate(_contract(checks=(
        ("ruff", "skipped", "no applicable target"),
        ("pytest", "skipped", "no_tests_collected"),
        ("mypy", "skipped", "empty_target_set_proven"))), authorized_snapshot_id="a" * 64)
    assert all_skipped.state is gate_states.GateState.INVALID_EVIDENCE
    assert all_skipped.authorized_to_ship is False, (
        "an inventory in which nothing executed must not authorize")


# ---------------------------------------------------- 11 stale snapshot

def test_11_post_audit_modification_is_stale_review(tmp_path: Path,
                                                    monkeypatch) -> None:
    """Case 11: modifying an included file AFTER the audit was taken makes
    the decision STALE_REVIEW and denies -- the checks did not consume the
    audited state."""
    repo = _make_repo(tmp_path)
    real_run = ship_gate.check_runner.run

    def mutating_run(root: Path, resolved: dict,
                    **_: object) -> list[dict]:
        results = real_run(root, resolved)
        _write(root, "README.md", "# mutated while checks ran\n")
        return results

    monkeypatch.setattr(ship_gate.check_runner, "run", mutating_run)
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo, reviewer=_clean_reviewer)
    assert "STALE_REVIEW" in str(exc.value)
    sealed = _sealed(repo)
    assert sealed["authorized_to_ship"] is False
    assert sealed["gate_state"] == "STALE_REVIEW"


def test_11b_evaluate_denies_on_snapshot_mismatch() -> None:
    """Case 11: the contract-level binding check itself."""
    decision = gate_states.evaluate(_contract(snapshot_id="a" * 64),
                                    authorized_snapshot_id="b" * 64)
    assert decision.state is gate_states.GateState.STALE_REVIEW
    assert decision.authorized_to_ship is False
    match = gate_states.evaluate(_contract(snapshot_id="a" * 64),
                                 authorized_snapshot_id="a" * 64)
    assert match.authorized_to_ship is True


# ------------------------------------------------- 12 frozen execution

def test_12_checks_that_mutate_the_target_are_detected(tmp_path: Path,
                                                       monkeypatch) -> None:
    """Case 12: the audited snapshot is taken BEFORE the checks and
    re-derived AFTER them. A check run that mutated the target therefore
    cannot authorize: the mismatch is detected and denied.

    LIMITATION (recorded in the phase report): this is detection, not
    materialization. Phase 1 does not copy the source into a frozen
    workspace; it proves the checks consumed the audited bytes or fails
    closed. Stronger immutability is deferred (it would require relocating
    execution, touching the single-runtime-authority invariant).
    """
    repo = _make_repo(tmp_path)
    audited = audit_snapshot.snapshot_id_of(repo)
    real_run = ship_gate.check_runner.run

    def mutating_run(root: Path, resolved: dict,
                    **_: object) -> list[dict]:
        results = real_run(root, resolved)
        _write(root, "pkg/leaf.py", "VALUE = 99\n")
        return results

    monkeypatch.setattr(ship_gate.check_runner, "run", mutating_run)
    with pytest.raises(evidence.EvidenceError):
        ship_gate.gate_ship(repo, reviewer=_clean_reviewer)
    sealed = _sealed(repo)
    assert sealed["gate_state"] == "STALE_REVIEW"
    assert sealed["snapshot_id"] == audited, (
        "evidence must record the AUDITED snapshot, not the post-check one")
    assert audit_snapshot.snapshot_id_of(repo) != audited, (
        "sanity: the target really did change")


# ------------------------------------------------ 13 single auth path

def test_13_only_completed_clean_authorizes() -> None:
    """Case 13: the authorization set that is unconditional across
    policies is exactly one state, and findings follow the severity
    policy: BLOCKER/HIGH deny; MEDIUM/LOW-only findings authorize under
    phase2 with the state recorded; phase1 still denies all findings."""
    assert gate_states.STATE_ALLOWS_SHIP == frozenset(
        {gate_states.GateState.COMPLETED_CLEAN})
    for state in gate_states.GateState:
        if state is not gate_states.GateState.COMPLETED_CLEAN:
            assert state not in gate_states.STATE_ALLOWS_SHIP

    # phase2, blocking severity -> deny
    blocked = gate_states.evaluate(_contract(findings=(_finding("BLOCKER"),)),
                                   authorized_snapshot_id="a" * 64)
    assert blocked.state is gate_states.GateState.COMPLETED_WITH_FINDINGS
    assert blocked.authorized_to_ship is False
    assert blocked.blocking_findings

    # phase2, non-blocking severity -> authorize WITH the state recorded
    # and the rule documented in gate_reasons (approved Phase 2 policy).
    allowed = gate_states.evaluate(_contract(findings=(_finding("MEDIUM"),)),
                                   authorized_snapshot_id="a" * 64)
    assert allowed.state is gate_states.GateState.COMPLETED_WITH_FINDINGS
    assert allowed.authorized_to_ship is True
    assert "SEVERITY_BLOCKS_SHIP" in allowed.reasons[0]

    # phase1 (legacy policy): findings never authorize on their own
    legacy = gate_states.evaluate(
        _contract(findings=(_finding("LOW"),), policy_version="phase1"),
        authorized_snapshot_id="a" * 64)
    assert legacy.state is gate_states.GateState.COMPLETED_WITH_FINDINGS
    assert legacy.authorized_to_ship is False


def test_13b_no_second_authorization_path_in_source() -> None:
    """Case 13: no module may hard-code an authorizing literal. The gate
    DERIVES the boolean; a literal True would be a bypass path.

    Scanned with an AST walk over assignments and dict literals, not a text
    regex: a regex matches its own warning prose (this test's first draft
    flagged a docstring sentence that merely MENTIONED the shape).
    """
    import ast

    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "asha").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Name)
                            and target.id == "authorized_to_ship"
                            and isinstance(node.value, ast.Constant)
                            and node.value.value is True):
                        offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant)
                            and key.value == "authorized_to_ship"
                            and isinstance(value, ast.Constant)
                            and value.value is True):
                        offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"hard-coded authorization literal(s): {offenders}"

    # positive control: the scan really detects the shape it bans
    probe = ast.parse("payload = {'authorized_to_ship': True}\n")
    hits = [n for n in ast.walk(probe)
            if isinstance(n, ast.Dict)
            and any(isinstance(k, ast.Constant)
                    and k.value == "authorized_to_ship"
                    for k in n.keys)]
    assert hits, "the AST probe must detect the banned shape"


def test_13d_findings_are_structural_and_never_silently_dropped() -> None:
    """F2: findings are an object list; non-dict findings must DENY at the
    contract boundary, never vanish into a clean authorization."""
    import pytest as _pt
    for bad in ("a string", 7, None, ["nested"]):
        with _pt.raises(gate_states.GateContractError):
            gate_states.validate_evidence({
                "snapshot_id": "a" * 64, "evidence_schema": evidence.SCHEMA,
                "policy_version": ship_gate.POLICY_VERSION,
                "required_checks": ["ruff"],
                "checks": [{"name": "ruff", "status": "passed",
                            "exit_code": 0}],
                "findings": [bad],
            })
    # mixed valid + invalid findings -> reject the ENTIRE payload
    with _pt.raises(gate_states.GateContractError):
        gate_states.validate_evidence({
            "snapshot_id": "a" * 64, "evidence_schema": evidence.SCHEMA,
            "policy_version": ship_gate.POLICY_VERSION,
            "required_checks": ["ruff"],
            "checks": [{"name": "ruff", "status": "passed",
                        "exit_code": 0}],
            "findings": [{"rule": "R1", "path": "pkg/leaf.py"}, "oops"],
        })


def test_13e_exit_code_contract_rejects_inconsistent_evidence() -> None:
    """F4 + F1: status and exit code must agree; a `passed` check with a
    non-zero code, a `failed` check with 0, or a `skipped` check with a
    non-zero/non-0 exit code is contradictory and is rejected at
    construction. Only exit_code==0 is a proven skip at this boundary."""
    import pytest as _pt
    for bad in (1, 127, 2, -1):
        with _pt.raises(gate_states.GateContractError):
            gate_states.CheckResult(name="ruff", status="passed",
                                    exit_code=bad)
    with _pt.raises(gate_states.GateContractError):
        gate_states.CheckResult(name="ruff", status="passed", exit_code=None)
    with _pt.raises(gate_states.GateContractError):
        gate_states.CheckResult(name="ruff", status="failed", exit_code=0)
    # F1: a skipped check with a non-0 exit code is NOT a proven skip.
    # cast() marks the deliberate contract violation: the values are
    # being fed through the typed signature on purpose, to prove the
    # runtime rejection.
    for bad_skip_code in (None, 5, 1, -1, "x", True, False):
        with _pt.raises(gate_states.GateContractError):
            gate_states.CheckResult(name="ruff", status="skipped",
                                    exit_code=cast(int | None,
                                                   bad_skip_code))
    # valid shapes pass construction
    gate_states.CheckResult(name="ruff", status="passed", exit_code=0)
    gate_states.CheckResult(name="ruff", status="failed", exit_code=1)
    gate_states.CheckResult(name="ruff", status="failed", exit_code=None)
    gate_states.CheckResult(name="ruff", status="skipped", exit_code=0)


def test_13g_forged_single_skip_denies() -> None:
    """F1 regression: a SKIPPED check that never ran must not authorize
    shipping. The original vulnerability: `is_proven_skip` checked only
    status, so `passed, passed, skipped(None)` reached COMPLETED_CLEAN /
    authorized_to_ship=True. Now a skipped check with a non-0 exit code
    is rejected at construction, and a skipped check with exit_code=0 is
    only trusted when the inventory is otherwise complete."""
    import pytest as _pt

    # 1) skipped + None (forged: check never executed) -> denied
    with _pt.raises(gate_states.GateContractError):
        gate_states.CheckResult(name="mypy", status="skipped", exit_code=None)

    # 2) the full inventory with a forged skip -> denied (payload path)
    def build(skip_code):
        return {"snapshot_id": "a" * 64, "evidence_schema": evidence.SCHEMA,
                "policy_version": ship_gate.POLICY_VERSION,
                "required_checks": ["ruff", "pytest", "mypy"],
                "checks": [{"name": "ruff", "status": "passed", "exit_code": 0},
                           {"name": "pytest", "status": "passed", "exit_code": 0},
                           {"name": "mypy", "status": "skipped",
                            "exit_code": skip_code}],
                # phase2: a clean, identity-bound review artifact so the
                # FORGED SKIP itself (not the missing artifact) decides
                "findings": [],
                "semantic_review": {"schema": "semantic-review/v1",
                                    "snapshot_id": "a" * 64,
                                    "policy_version": ship_gate.POLICY_VERSION,
                                    "status": "ok", "findings": []}}
    for bad_code in (None, 5, 1, -1, "forged-string", True, False):
        with _pt.raises(gate_states.GateContractError):
            gate_states.validate_evidence(build(bad_code))

    # 3) a REAL skip (exit_code==0, empty-argv proof) still flies
    v = gate_states.validate_evidence(build(0))
    decision = gate_states.evaluate(v, authorized_snapshot_id="a" * 64)
    assert decision.state is gate_states.GateState.COMPLETED_CLEAN
    assert decision.authorized_to_ship is True


def test_13h_unproven_skip_denial_not_dead() -> None:
    """F1: the deny path is live -- confirm the construction rejection is
    what stops an unproven skip, and that evaluate() itself is also
    unreachable-by-construction (a skipped entry can only exist with
    exit_code==0, which is the proven shape)."""
    import pytest as _pt
    # a skipped check with no exit code cannot be built at all
    with _pt.raises(gate_states.GateContractError):
        gate_states.CheckResult(name="mypy", status="skipped", exit_code=None)
    # and the all-skipped inventory still denies at the decision layer
    all_skipped = gate_states.evaluate(_contract(checks=(
        ("ruff", "skipped", "no applicable target"),
        ("pytest", "skipped", "no_tests_collected"),
        ("mypy", "skipped", "empty_target_set_proven"))), authorized_snapshot_id="a" * 64)
    assert all_skipped.state is gate_states.GateState.INVALID_EVIDENCE
    assert all_skipped.authorized_to_ship is False


def test_13i_validate_evidence_rejects_malformed_exit_codes() -> None:
    """F1/C: validate_evidence must reject foreign exit-code types, not
    normalize them into a valid skip."""
    import pytest as _pt
    base = {"snapshot_id": "a" * 64, "evidence_schema": evidence.SCHEMA,
            "policy_version": ship_gate.POLICY_VERSION,
            "required_checks": ["ruff"],
            "checks": [{"name": "ruff", "status": "passed",
                        "exit_code": 0}]}
    bad_values: tuple[object, ...] = ("0", "ok", 1.5, [], {}, True, False)
    for bad in bad_values:
        payload = dict(base)
        payload["checks"] = [{"name": "ruff", "status": "passed",
                              "exit_code": bad}]
        with _pt.raises(gate_states.GateContractError):
            gate_states.validate_evidence(payload)


def test_13f_all_skipped_inventory_denies() -> None:
    """F1 complement: an inventory in which NOTHING executed -- every
    check skipped -- must not authorize shipping (the all([]) fail-open,
    reached through the skip path)."""
    all_skipped = gate_states.evaluate(_contract(checks=(
        ("ruff", "skipped", "no applicable target"),
        ("pytest", "skipped", "no_tests_collected"),
        ("mypy", "skipped", "empty_target_set_proven"))), authorized_snapshot_id="a" * 64)
    assert all_skipped.state is gate_states.GateState.INVALID_EVIDENCE
    assert all_skipped.authorized_to_ship is False


def test_13c_gate_state_is_derived_not_accepted(tmp_path: Path,
                                                monkeypatch) -> None:
    """Case 13: an evidence payload claiming authorization cannot install
    it -- the gate recomputes the state from the checks it ran."""
    repo = _make_repo(tmp_path)

    def padded_run(root: Path, resolved: dict,
                  **_: object) -> list[dict]:
        return [
            {"name": name, "status": "passed", "exit_code": 0, "note": ""}
            for name in resolved["checks"]
        ]

    monkeypatch.setattr(ship_gate.check_runner, "run", padded_run)
    payload = ship_gate.gate_ship(repo, reviewer=_clean_reviewer)
    assert payload["authorized_to_ship"] is True
    assert payload["gate_state"] == "COMPLETED_CLEAN"
    assert payload["gate_reasons"] == []
    # the sealed digest covers the derived fields, so a hand-edited state
    # breaks verification instead of granting authorization
    tampered = dict(payload)
    tampered["gate_state"] = "COMPLETED_WITH_FINDINGS"
    assert evidence.compute_digest(tampered) != payload["evidence_sha256"]


# --------------------------------------------------- 14 positive control

def test_14_complete_valid_clean_audit_authorizes(tmp_path: Path) -> None:
    """Case 14: an unchanged, fully-checked snapshot still ships. The
    hardening must not make a legitimate run unshippable."""
    repo = _make_repo(tmp_path)
    payload = ship_gate.gate_ship(repo, reviewer=_clean_reviewer)
    assert payload["authorized_to_ship"] is True
    assert payload["gate_state"] == "COMPLETED_CLEAN"
    assert payload["snapshot_id"] == audit_snapshot.snapshot_id_of(repo)
    assert payload["policy_version"] == ship_gate.POLICY_VERSION
    assert payload["manifest_version"] == audit_snapshot.MANIFEST_VERSION
    assert evidence.compute_digest(payload) == payload["evidence_sha256"]
    statuses = {check["name"]: check["status"] for check in payload["checks"]}
    assert statuses, "a completed run must record its checks"
    # the required inventory is recorded verbatim and no check failed
    assert set(statuses.values()) <= {"passed", "skipped"}
    assert "failed" not in statuses.values()
    # every skip must be JUSTIFIED by the runner (never a silent no-op)
    for name, status in statuses.items():
        if status == "skipped":
            entry = next(c for c in payload["checks"] if c["name"] == name)
            assert entry.get("exit_code") == 0, (
                f"{name} skipped without the runner's recorded exit code; "
                "a proven skip carries 0 (empty-argv proof), anything "
                "else is an execution that never completed")


def test_14b_tampered_artifact_still_denies(tmp_path: Path) -> None:
    """Case 14/13: the pre-existing tamper contract is preserved by the
    hardened gate. The fixture is a repo whose checks FAIL (so the gate
    seals authorized_to_ship=False), then the artifact is edited to claim
    authorization without resealing the digest -> rejected."""
    repo = _make_repo(tmp_path)
    _write(repo, "bad.py", "import os\n")  # ruff F401 -> failing check
    _commit_all(repo, "introduce lint failure")
    with pytest.raises(evidence.EvidenceError):
        ship_gate.gate_ship(repo, reviewer=_clean_reviewer)
    artifact = _evidence_file(repo)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["authorized_to_ship"] is False
    # Manual tamper: flip authorization (and the typed state) without
    # resealing the digest.
    payload["authorized_to_ship"] = True
    payload["gate_state"] = "COMPLETED_CLEAN"
    artifact.write_text(json.dumps(payload, indent=2, sort_keys=True),
                        encoding="utf-8")
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo, reviewer=_clean_reviewer)
    assert "evidence_sha256" in str(exc.value) or "mismatch" in str(exc.value)


# ------------------------------- 15 Stage 4: rule reuse, no redundant rule

def test_15_syntax_error_in_audited_snapshot_denies(tmp_path: Path) -> None:
    """Stage 4: syntax validity is ALREADY enforced fail-closed by the
    existing ruff check (ruff reports `invalid-syntax` -> nonzero ->
    status 'failed' -> EXECUTION_FAILED -> denial), so Phase 1 adds NO
    second syntax rule. This test is the evidence for that decision: it
    drives the real gate over a snapshot containing a file that cannot be
    parsed and asserts denial. Had a new py_compile rule been added, this
    test would pass on a duplicated verdict -- that redundancy is exactly
    why it was not added.
    """
    repo = _make_repo(tmp_path)
    _write(repo, "pkg/broken.py", "def broken(:\n    pass\n")
    _commit_all(repo, "introduce syntax error")
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo, reviewer=_clean_reviewer)
    assert "EXECUTION_FAILED" in str(exc.value), str(exc.value)
    sealed = _sealed(repo)
    assert sealed["authorized_to_ship"] is False
    assert sealed["gate_state"] == "EXECUTION_FAILED"
    failing = [c for c in sealed["checks"] if c["status"] == "failed"]
    assert failing, "the syntax error must be attributed to a real check"
    assert any("invalid-syntax" in c.get("output_tail", "")
               for c in failing), (
        "the failure evidence must name the syntax problem")
