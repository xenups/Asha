"""Phase 3 (Stage 3): live LLM reviewer adapter integration tests.

Covers, without any real network:

* valid JSON findings of every severity parse and partition correctly
  (adapter output -> Phase 2 gate evaluation);
* malformed JSON, HTTP rate limits and timeouts map to safe denial
  (EXECUTION_FAILED) end to end through gate_ship;
* snapshot_id binding: absent echo fails as malformed output, a
  wrong echo is rejected by the gate (INVALID_EVIDENCE);
* live configuration is fail-closed: requested-without-credentials
  denies INVALID_EVIDENCE (never a mock pass), flag off resolves no
  executor at all;
* the outgoing prompt carries the snapshot_id, the spec contract and
  digest-verified file context within the configured budget.

Transport is injected by monkeypatching live_reviewer._http_post (the
same seam ship_gate's injected reviewer uses); no sockets are opened.
"""

from __future__ import annotations

import email.message
import json
import sys
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from asha import evidence, gate_states
from asha.common import paths as common_paths
from asha.governance import live_reviewer, semantic_review, ship_gate

GITIGNORE = (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
             ".mypy_cache/\n.ruff_cache/\nvenv/\n")
BASELINE = {
    ".gitignore": GITIGNORE,
    "README.md": "# fixture\n",
    "pyproject.toml": "[tool.ruff]\nline-length = 88\n",
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    "pkg/leaf.py": "VALUE = 1\n",
}

SNAPSHOT = "a" * 64


# --------------------------------------------------------------- fixtures

def _git(repo: Path, *args: str) -> None:
    import subprocess
    subprocess.run(["git", *args], cwd=repo, capture_output=True,
                   text=True, timeout=60, check=True)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in BASELINE.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _sealed(repo: Path) -> dict:
    path = common_paths.get_evidence_dir(repo) / "evidence.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _finding(severity: str = "MEDIUM", **overrides: str) -> dict:
    entry = {
        "severity": severity,
        "file_path": "pkg/leaf.py",
        "line_range": "1-2",
        "problem": "test problem",
        "why_it_matters": "test consequence",
        "failure_scenario": "test scenario",
        "recommended_fix": "test fix",
    }
    entry.update(overrides)
    return entry


def _llm_reply(findings: list[dict], snapshot_id: str = SNAPSHOT,
               echo: str | None = "sentinel") -> dict:
    """A canned OpenAI-style response carrying one review document."""
    document: dict = {"findings": findings}
    if echo == "sentinel":  # default: echo the snapshot_id we sent
        echo = snapshot_id
    if echo is not None:    # None -> omit the key entirely
        document["snapshot_id"] = echo
    return {
        "choices": [{"message": {"content": json.dumps(document)}}],
        "usage": {"total_tokens": 123},
    }


def _capturing_transport(reply: object):
    """Fake _http_post that records (url, payload, config) and replies."""
    captured: dict[str, object] = {}

    def _fake(url: str, payload: dict, config: object) -> object:
        captured["url"] = url
        captured["payload"] = payload
        captured["config"] = config
        result = reply() if callable(reply) else reply
        if isinstance(result, Exception):
            raise result
        return result

    _fake.captured = captured  # type: ignore[attr-defined]
    return _fake


@pytest.fixture
def stub_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parsing-focused tests do not need a git repo under them."""
    monkeypatch.setattr(
        live_reviewer, "_review_context",
        lambda root, budget: "(no changed files in this snapshot)")


@pytest.fixture
def live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASHA_SEMANTIC_REVIEW", "live")
    monkeypatch.setenv("ASHA_LLM_API_KEY", "test-key")
    monkeypatch.setenv("ASHA_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("ASHA_LLM_MODEL", "test-model")


def _echo_transport(findings: list[dict]):
    """Transport that echoes back the snapshot_id the gate actually sent
    (extracted from the outgoing prompt) -- the realistic LLM behavior."""
    def _fake(url, payload, config):
        user = payload["messages"][1]["content"]
        requested = user.split("snapshot_id: ", 1)[1].split("\n", 1)[0]
        return _llm_reply(findings, snapshot_id=requested.strip())
    return _fake


def _evaluate_artifact(artifact: dict, findings: list[dict]
                       ) -> gate_states.GateDecision:
    """Drive the adapter's artifact through the Phase 2 contract."""
    evidence_payload = {
        "snapshot_id": SNAPSHOT,
        "evidence_schema": evidence.SCHEMA,
        "policy_version": "phase2",
        "required_checks": ["ruff", "pytest", "mypy", "semantic-review"],
        "checks": [
            {"name": name, "status": "passed", "exit_code": 0}
            for name in ("ruff", "pytest", "mypy", "semantic-review")
        ],
        "findings": findings,
        "semantic_review": artifact,
    }
    return gate_states.evaluate(
        gate_states.validate_evidence(evidence_payload),
        authorized_snapshot_id=SNAPSHOT)


# ------------------------------------------- A. resolution & configuration

def test_live_env_resolves_executor(live_env: None) -> None:
    assert (live_reviewer.resolve_live_reviewer()
            is live_reviewer.run_live_semantic_reviewer)


def test_resolve_requires_flag_and_key(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # flag without key -> no executor (fail closed downstream)
    monkeypatch.setenv("ASHA_SEMANTIC_REVIEW", "live")
    monkeypatch.delenv("ASHA_LLM_API_KEY", raising=False)
    assert live_reviewer.resolve_live_reviewer() is None
    # key without flag -> inert
    monkeypatch.delenv("ASHA_SEMANTIC_REVIEW", raising=False)
    monkeypatch.setenv("ASHA_LLM_API_KEY", "k")
    assert live_reviewer.resolve_live_reviewer() is None


def test_config_clamps_timeout_and_bounds_spend(
        live_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASHA_LLM_TIMEOUT_S", "9999")
    monkeypatch.setenv("ASHA_LLM_MAX_TOKENS", "500")
    config = live_reviewer._config_from_env(timeout_s=5)
    assert config.timeout_s == 5  # clamped to the caller's budget
    assert config.max_tokens == 500  # spending ceiling honored
    assert config.temperature == 0.0
    monkeypatch.setenv("ASHA_LLM_TIMEOUT_S", "not-a-number")
    with pytest.raises(live_reviewer.ReviewerError):
        live_reviewer._config_from_env(timeout_s=5)


# ------------------------------------------- B. parsing & severity partition

@pytest.mark.parametrize("severity,authorizes", [
    ("BLOCKER", False), ("HIGH", False),
    ("MEDIUM", True), ("LOW", True),
])
def test_valid_findings_parse_and_partition(
        live_env: None, stub_context: None,
        monkeypatch: pytest.MonkeyPatch,
        severity: str, authorizes: bool) -> None:
    finding = _finding(severity)
    monkeypatch.setattr(
        live_reviewer, "_http_post",
        _capturing_transport(_llm_reply([finding])))
    artifact = live_reviewer.run_live_semantic_reviewer(
        Path("/unused"), SNAPSHOT, 900)
    assert artifact["schema"] == semantic_review.REVIEW_SCHEMA
    assert artifact["status"] == "live"
    assert artifact["model"] == "test-model"
    assert artifact["usage"] == {"total_tokens": 123}
    decision = _evaluate_artifact(artifact, [finding])
    assert decision.state is gate_states.GateState.COMPLETED_WITH_FINDINGS
    assert decision.authorized_to_ship is authorizes
    assert bool(decision.blocking_findings) is (not authorizes)


def test_prompt_binds_snapshot_spec_and_contract(
        live_env: None, stub_context: None,
        monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _capturing_transport(_llm_reply([]))
    monkeypatch.setattr(live_reviewer, "_http_post", transport)
    live_reviewer.run_live_semantic_reviewer(Path("/unused"), SNAPSHOT, 900)
    captured = transport.captured
    assert captured["url"] == "https://llm.test/v1/chat/completions"
    system = captured["payload"]["messages"][0]["content"]
    user = captured["payload"]["messages"][1]["content"]
    assert f"snapshot_id: {SNAPSHOT}" in user
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["max_tokens"] == \
        live_reviewer.DEFAULT_MAX_TOKENS
    # the machine contract rides the prompt: fields + severity ladder
    for field in semantic_review.FINDING_FIELDS:
        assert f'"{field}"' in system
    assert "BLOCKER, HIGH, MEDIUM, LOW" in system


# ------------------------------------------- C. snapshot_id binding

def test_missing_snapshot_echo_fails_closed(
        live_env: None, stub_context: None,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        live_reviewer, "_http_post",
        _capturing_transport(_llm_reply([], echo=None)))
    with pytest.raises(live_reviewer.ReviewerError, match="snapshot_id"):
        live_reviewer.run_live_semantic_reviewer(
            Path("/unused"), SNAPSHOT, 900)


def test_wrong_snapshot_echo_reaches_gate_as_invalid(
        live_env: None, stub_context: None,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign echo is NOT repaired: it flows into the artifact so the
    gate's validate_artifact rejects it (INVALID_EVIDENCE)."""
    monkeypatch.setattr(
        live_reviewer, "_http_post",
        _capturing_transport(_llm_reply([], echo="b" * 64)))
    artifact = live_reviewer.run_live_semantic_reviewer(
        Path("/unused"), SNAPSHOT, 900)
    assert artifact["snapshot_id"] == "b" * 64
    with pytest.raises(gate_states.GateContractError):
        semantic_review.validate_artifact(
            artifact, snapshot_id=SNAPSHOT, policy_version="phase2")


# ------------------------------------------- D. malformed output handling

def test_malformed_json_raises_reviewer_error(
        live_env: None, stub_context: None,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        live_reviewer, "_http_post",
        _capturing_transport({"choices": [{"message": {
            "content": "{not json at all"}}]}))
    with pytest.raises(live_reviewer.ReviewerError, match="malformed JSON"):
        live_reviewer.run_live_semantic_reviewer(
            Path("/unused"), SNAPSHOT, 900)


def test_invalid_severity_raises_reviewer_error(
        live_env: None, stub_context: None,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        live_reviewer, "_http_post",
        _capturing_transport(_llm_reply([_finding("CRITICAL")])))
    with pytest.raises(live_reviewer.ReviewerError, match="schema"):
        live_reviewer.run_live_semantic_reviewer(
            Path("/unused"), SNAPSHOT, 900)


def test_transport_errors_raise_reviewer_error(
        live_env: None, stub_context: None,
        monkeypatch: pytest.MonkeyPatch) -> None:
    rate_limited = urllib.error.HTTPError(
        "https://llm.test", 429, "Too Many Requests",
        email.message.Message(), None)
    for failure in (rate_limited, TimeoutError("timed out"),
                    urllib.error.URLError("dns down")):
        monkeypatch.setattr(
            live_reviewer, "_http_post", _capturing_transport(failure))
        with pytest.raises(live_reviewer.ReviewerError):
            live_reviewer.run_live_semantic_reviewer(
                Path("/unused"), SNAPSHOT, 900)


# ------------------------------------------- E. context construction

def test_context_is_digest_verified_and_budgeted(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # pretend the resolver sees one changed file
    monkeypatch.setattr(
        live_reviewer.scope_resolver, "resolve",
        lambda root: {"affected_files": ["pkg/leaf.py"]})
    context = live_reviewer._review_context(repo, budget=60_000)
    assert "### FILE: pkg/leaf.py" in context
    assert "VALUE = 1" in context
    # budget: truncate aggressively
    tight = live_reviewer._review_context(repo, budget=5)
    assert "truncated" in tight
    # digest mismatch -> fail closed, never review unverifiable bytes
    entry = audit_manifest_entry(path="pkg/leaf.py", digest="0" * 64)
    monkeypatch.setattr(
        live_reviewer.audit_snapshot, "build_manifest",
        lambda root: type("M", (), {"index": lambda self: {
            "pkg/leaf.py": entry}})())
    with pytest.raises(live_reviewer.ReviewerError, match="digest"):
        live_reviewer._review_context(repo, budget=60_000)


def audit_manifest_entry(path: str, digest: str):
    return live_reviewer.audit_snapshot.ManifestEntry(
        path=path, entry_type="file", digest=digest, size=1)


# ------------------------------------------- F. end-to-end gate denial

def test_live_gate_malformed_json_denies_execution_failed(
        live_env: None, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(
        live_reviewer, "_http_post",
        _capturing_transport({"choices": [{"message": {"content": "nope"}}]}))
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo)
    assert "EXECUTION_FAILED" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


def test_live_gate_rate_limit_denies_execution_failed(
        live_env: None, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(
        live_reviewer, "_http_post",
        _capturing_transport(urllib.error.HTTPError(
            "https://llm.test", 429, "Too Many",
            email.message.Message(), None)))
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo)
    assert "EXECUTION_FAILED" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


def test_live_gate_timeout_denies_execution_failed(
        live_env: None, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(
        live_reviewer, "_http_post",
        _capturing_transport(TimeoutError("LLM timed out")))
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo)
    assert "EXECUTION_FAILED" in str(exc.value)
    sealed = _sealed(repo)
    assert sealed["authorized_to_ship"] is False
    review = [c for c in sealed["checks"]
              if c["name"] == "semantic-review"]
    assert review and review[0]["status"] == "failed"


def test_live_gate_snapshot_echo_mismatch_invalid(
        live_env: None, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(
        live_reviewer, "_http_post",
        _capturing_transport(_llm_reply([], echo="b" * 64)))
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo)
    assert "INVALID_EVIDENCE" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


def test_live_requested_without_credentials_denies_invalid(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Live review requested but no key: NO executor resolves, so the
    phase2 missing-artifact path denies -- never a silent mock pass."""
    monkeypatch.setenv("ASHA_SEMANTIC_REVIEW", "live")
    monkeypatch.delenv("ASHA_LLM_API_KEY", raising=False)
    repo = _make_repo(tmp_path)
    with pytest.raises(evidence.EvidenceError) as exc:
        ship_gate.gate_ship(repo)
    assert "INVALID_EVIDENCE" in str(exc.value)
    assert _sealed(repo)["authorized_to_ship"] is False


def test_live_gate_clean_review_authorizes(
        live_env: None, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path) -> None:
    """Full happy path: live resolution -> transport -> artifact ->
    COMPLETED_CLEAN authorization (proves the wiring is usable)."""
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(
        live_reviewer, "_http_post", _echo_transport([]))
    payload = ship_gate.gate_ship(repo)
    assert payload["authorized_to_ship"] is True
    assert payload["gate_state"] == "COMPLETED_CLEAN"
    assert payload["semantic_review"]["status"] == "live"


def test_flag_off_resolves_nothing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASHA_SEMANTIC_REVIEW", raising=False)
    assert live_reviewer.resolve_live_reviewer() is None
