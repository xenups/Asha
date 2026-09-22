"""Tests for the institutional memory layer (memory.py).

Adapter boundary is isolated: deterministic DictBackend for logic tests,
plus one roundtrip against the REAL installed mem0 package (skips with a
verbatim reason if it cannot initialise). Precedence is mandatory:
current ORIENT facts always win; conflicting repository facts become
stale and are never deleted.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / ".hermes" / "tools"
CONTROL = REPO_ROOT / ".jspace" / "control.py"
PY = sys.executable

if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import memory  # (TOOLS must be on sys.path before this import)
import project_map

GITIGNORE = (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
             ".mypy_cache/\n.ruff_cache/\n")

PYPROJECT_TEMPLATE = """[project]
name = "fixture-app"
requires-python = ">=3.11"
dependencies = ["{framework}>=0.100"]

[tool.ruff]
line-length = 88

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.mypy]
strict = false
"""

CONTEXT_KEYS = {"schema", "current_facts", "memory"}
MEMORY_KEYS = {"repository_facts", "workflow_preferences",
               "historical_lessons", "decision_records",
               "stale_repository_facts"}


class DictBackend:
    """Deterministic adapter-boundary double (same duck-typed surface as
    Mem0Backend; records arrive pre-normalised)."""

    available = True
    reason = None

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}

    def add(self, content: str, *, user_id: str,
            metadata: dict) -> str:
        mid = str(uuid.uuid4())
        self.store[mid] = {"id": mid, "content": content,
                           "metadata": dict(metadata),
                           "created_at": memory._utc_now(),
                           "updated_at": None, "user_id": user_id}
        return mid

    def search(self, query: str, *, user_id: str,
               top_k: int) -> list[dict]:
        return [dict(r) for r in self.store.values()
                if r["user_id"] == user_id][:top_k]

    def get_all(self, *, user_id: str) -> list[dict]:
        return [dict(r) for r in self.store.values()
                if r["user_id"] == user_id]

    def update(self, memory_id: str, *, text: str | None = None,
               metadata: dict | None = None) -> None:
        record = self.store[memory_id]
        if text is not None:
            record["content"] = text
        if metadata is not None:
            record["metadata"] = dict(metadata)
        record["updated_at"] = memory._utc_now()

    def delete(self, memory_id: str) -> None:
        del self.store[memory_id]


class BrokenBackend:
    """Simulates a mem0 that initialised but fails at runtime."""

    available = True
    reason = None

    def __getattr__(self, name):
        def explode(*args, **kwargs):
            raise RuntimeError("simulated backend failure")
        return explode


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


def _make_repo(tmp_path: Path, framework: str = "fastapi") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "fixture@example.com")
        _git(repo, "config", "user.name", "Fixture")
        _git(repo, "config", "commit.gpgsign", "false")
        _write(repo, ".gitignore", GITIGNORE)
        _write(repo, "pyproject.toml",
               PYPROJECT_TEMPLATE.format(framework=framework))
        _write(repo, "tests/test_ok.py", "def test_ok():\n    assert True\n")
        _commit_all(repo, "baseline")
    return repo


def _orient(repo: Path) -> dict:
    return project_map.build(repo, "quick", use_cache=False)


def _tree(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()


# ---- 1. current fact wins ------------------------------------------------

def test_current_fact_wins_over_memory(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    store = DictBackend()
    memory.add_memory(
        repo, "early scaffolding used unittest", "repository_fact",
        fact_key="tooling.test_runner", fact_value="unittest",
        backend=store)
    orient = _orient(repo)
    assert orient["tooling"]["test_runner"]["value"] == "pytest"

    context = memory.build_context(orient, memory.get_all_memories(
        repo, backend=store))

    assert context["current_facts"]["tooling"]["test_runner"]["value"] == \
        "pytest"
    stale = context["memory"]["stale_repository_facts"]
    assert len(stale) == 1
    assert stale[0]["metadata"]["fact_value"] == "unittest"
    assert stale[0]["conflict"] is True
    assert stale[0]["current_value"] == "pytest"
    assert stale[0]["status"] == "stale"
    # the stale value never leaks into current facts:
    assert "unittest" not in json.dumps(context["current_facts"])


# ---- 2. non-conflicting memory survives ----------------------------------

def test_non_conflicting_memory_survives(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    store = DictBackend()
    memory.add_memory(
        repo, "tests are pytest", "repository_fact",
        fact_key="tooling.test_runner", fact_value="pytest", backend=store)
    memory.add_memory(
        repo, "schema changes previously required package-wide regression",
        "historical_lesson", backend=store)

    context = memory.build_context(_orient(repo),
                                   memory.get_all_memories(repo,
                                                           backend=store))
    assert len(context["memory"]["repository_facts"]) == 1
    assert context["memory"]["stale_repository_facts"] == []
    lessons = [r["content"]
               for r in context["memory"]["historical_lessons"]]
    assert any("package-wide regression" in c for c in lessons)


# ---- 3. repository fact becomes stale after conflict ---------------------

def test_repo_fact_becomes_stale_after_conflict(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, framework="fastapi")
    store = DictBackend()
    record = memory.add_memory(
        repo, "framework was fastapi", "repository_fact",
        fact_key="stack.framework", fact_value="fastapi", backend=store)
    assert record["metadata"]["tree_hash"] == _tree(repo), (
        "repository facts bind to the observed tree")

    # change repository state: fastapi -> flask
    _write(repo, "pyproject.toml",
           PYPROJECT_TEMPLATE.format(framework="flask"))
    _commit_all(repo, "switch to flask")
    orient = _orient(repo)
    assert orient["stack"]["framework"]["value"] == "flask"

    grouped = memory.validate_against_orient(repo, orient, backend=store)
    stale = grouped["stale_repository_facts"]
    assert len(stale) == 1
    assert stale[0]["conflict"] is True
    assert stale[0]["current_value"] == "flask"
    assert stale[0]["status"] == "stale"
    # persisted, not deleted:
    assert store.store[record["id"]]["metadata"]["status"] == "stale"
    assert record["id"] in store.store
    # old tree binding preserved as history:
    assert store.store[record["id"]]["metadata"]["tree_hash"] != _tree(repo)


# ---- 4. workflow preferences survive tree changes ------------------------

def test_workflow_preference_survives_tree_change(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    store = DictBackend()
    memory.add_memory(repo, "prefer conventional commits",
                      "workflow_preference", backend=store)
    _write(repo, "docs.md", "# docs\n")
    _commit_all(repo, "docs touch")

    records = memory.get_all_memories(repo, backend=store)
    assert len(records) == 1
    assert records[0]["metadata"]["status"] == "active"
    # not artificially tree-bound:
    assert "tree_hash" not in records[0]["metadata"]
    context = memory.build_context(_orient(repo), records)
    assert len(context["memory"]["workflow_preferences"]) == 1
    assert context["memory"]["stale_repository_facts"] == []


# ---- 5. historical lessons survive ---------------------------------------

def test_historical_lesson_survives(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    store = DictBackend()
    memory.add_memory(
        repo, "changing this schema required downstream tests",
        "historical_lesson", backend=store)
    _write(repo, "docs.md", "# docs\nmore\n")
    _commit_all(repo, "another change")

    context = memory.build_context(_orient(repo),
                                   memory.get_all_memories(repo,
                                                           backend=store))
    lessons = context["memory"]["historical_lessons"]
    assert len(lessons) == 1
    assert "downstream tests" in lessons[0]["content"]
    assert lessons[0]["status"] == "active"


# ---- 6. no memory override -----------------------------------------------

def test_no_path_lets_memory_override_current_facts(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    store = DictBackend()
    memory.add_memory(repo, "unittest era", "repository_fact",
                      fact_key="tooling.test_runner",
                      fact_value="unittest", backend=store)
    memory.add_memory(repo, "framework was flask", "repository_fact",
                      fact_key="stack.framework", fact_value="flask",
                      backend=store)
    orient = _orient(repo)
    context = memory.build_context(orient,
                                   memory.get_all_memories(repo,
                                                           backend=store))

    # current_facts is byte-identical to ORIENT (memory never mutates it):
    assert json.dumps(context["current_facts"], sort_keys=True) == \
        json.dumps(orient, sort_keys=True)
    assert context["current_facts"]["stack"]["framework"]["value"] == \
        "fastapi"
    stale = context["memory"]["stale_repository_facts"]
    assert len(stale) == 2
    for entry in stale:
        assert entry["metadata"]["fact_value"] != entry["current_value"]
    assert "flask" not in json.dumps(context["current_facts"])


# ---- 7. memory failure is non-fatal --------------------------------------

def test_memory_failure_non_fatal(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    # (a) unavailable backend: memory ops fail closed with one error type
    memory.set_backend(memory.NullBackend("simulated outage"))
    try:
        for op in (lambda: memory.add_memory(repo, "x", "decision_record"),
                   lambda: memory.search_memory(repo, "x")):
            try:
                op()
                raise AssertionError("expected MemoryLayerError")
            except memory.MemoryLayerError as exc:
                assert "MEMORY UNAVAILABLE" in str(exc)
    finally:
        memory.set_backend(None)

    # (b) broken-at-runtime backend: clean CLI error, no traceback leak
    #     (in-process: the backend override lives in this module only)
    memory.set_backend(BrokenBackend())
    try:
        import contextlib as _cl
        import io
        buf = io.StringIO()
        with _cl.redirect_stderr(buf):
            rc = memory.main(["--root", str(repo), "status"])
        assert rc == 1
        assert "MEMORY ERROR" in buf.getvalue()
    finally:
        memory.set_backend(None)

    # (c) ORIENT / SCOPE / GATE paths are untouched by memory state
    proc = subprocess.run(
        [PY, str(CONTROL), "--transport", "local", "--root", str(repo),
         "orient", "--mode", "quick"],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    proc = subprocess.run(
        [PY, str(TOOLS / "scope_resolver.py"), "--root", str(repo)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "scope=" in proc.stdout
    # gate reaches its OWN (expected) refusal -- not a memory crash:
    proc = subprocess.run(
        [PY, str(CONTROL), "--transport", "local", "--root", str(repo),
         "check", "--stage", "work"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1
    assert "control.json" in proc.stderr
    assert "memory" not in proc.stderr.lower()

    # (d) real resolve_backend never raises (Mem0 or Null):
    resolved = memory.resolve_backend(tmp_path / "fresh")
    assert isinstance(resolved, (memory.Mem0Backend, memory.NullBackend))


# ---- 8. scoped retrieval --------------------------------------------------

def test_scoped_retrieval(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    store = DictBackend()
    keep = memory.add_memory(
        repo, "manifest writer emits key files", "historical_lesson",
        backend=store)["id"]
    keep2 = memory.add_memory(
        repo, "manifest serialization round-trip tests exist",
        "decision_record", backend=store)["id"]
    memory.add_memory(
        repo, "vacation itinerary planning notes", "historical_lesson",
        backend=store)
    memory.add_memory(
        repo, "unrelated bundle tokenizer rewrite", "historical_lesson",
        backend=store)

    hits = memory.search_memory(repo, "change manifest serialization",
                                limit=3, backend=store)
    ids = [h["id"] for h in hits]
    assert keep in ids and keep2 in ids
    assert len(hits) <= 3
    contents = " ".join(h["content"] for h in hits)
    assert "vacation" not in contents and "tokenizer" not in contents


# ---- 9. sensitive-data protection -----------------------------------------

def test_sensitive_data_not_persisted(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    store = DictBackend()
    payloads = [
        "deploy key is sk-" + "a1B2" * 8,
        "aws id AKIA" + "ABCDEF0123456789",
        "password = supersecretvalue",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE",
        "auth token: " + "ghp_" + "x" * 36,
    ]
    for payload in payloads:
        try:
            memory.add_memory(repo, payload, "historical_lesson",
                              backend=store)
            raise AssertionError(f"refused expected: {payload[:24]!r}")
        except memory.MemoryLayerError as exc:
            assert "REFUSED" in str(exc)
    assert store.store == {}, "nothing secret-shaped may be persisted"


# ---- 10. serialization / schema stability ---------------------------------

def test_context_schema_stable(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    store = DictBackend()
    memory.add_memory(repo, "prefer small commits", "workflow_preference",
                      backend=store)
    memory.add_memory(repo, "unittest era", "repository_fact",
                      fact_key="tooling.test_runner",
                      fact_value="unittest", backend=store)
    context = memory.build_context(_orient(repo),
                                   memory.get_all_memories(repo,
                                                           backend=store))

    assert set(context) == CONTEXT_KEYS
    assert context["schema"] == 1
    assert set(context["memory"]) == MEMORY_KEYS
    json.dumps(context)  # fully serialisable
    for bucket in context["memory"].values():
        for record in bucket:
            assert {"id", "content", "metadata", "status"} <= set(record)
            assert record["metadata"]["category"] in memory.CATEGORIES
            assert record["metadata"]["status"] in ("active", "stale")


# ---- real installed Mem0 roundtrip + CLI smoke ----------------------------

def test_real_mem0_roundtrip_and_cli(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    backend = memory.resolve_backend(repo)
    if not backend.available:
        pytest.skip(f"real mem0 unavailable: {backend.reason}")

    record = memory.add_memory(
        repo, "manifest writer emits key files", "repository_fact",
        fact_key="tooling.test_runner", fact_value="pytest",
        backend=backend)
    hits = memory.search_memory(repo, "manifest writer", limit=5,
                                backend=backend)
    assert record["id"] in [h["id"] for h in hits]

    # stale marking roundtrip on the real store (never a delete):
    full = memory.get_all_memories(repo, backend=backend)
    target = next(r for r in full if r["id"] == record["id"])
    memory.mark_stale(repo, target, "demo conflict", backend=backend)
    after = next(r for r in memory.get_all_memories(repo, backend=backend)
                 if r["id"] == record["id"])
    assert after["metadata"]["status"] == "stale"

    memory.delete_memory(repo, record["id"], backend=backend)
    assert record["id"] not in [r["id"] for r in
                                memory.get_all_memories(repo,
                                                        backend=backend)]

    # CLI smoke (separate process, resolves the real backend itself):
    proc = subprocess.run(
        [PY, str(TOOLS / "memory.py"), "--root", str(repo), "add",
         "--category", "decision_record",
         "--content", "writer remains source of truth"],
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    added = json.loads(proc.stdout)
    assert added["metadata"]["category"] == "decision_record"
    proc = subprocess.run(
        [PY, str(TOOLS / "memory.py"), "--root", str(repo), "search",
         "--task", "writer source of truth"],
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert added["id"] in [r["id"] for r in
                           json.loads(proc.stdout)["retrieved"]]
