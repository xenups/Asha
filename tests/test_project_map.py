"""End-to-end tests for live project orientation (project_map.py).

Real scratch git repositories where git state matters; every claim checked
against observable fixture state. Facts must carry provenance; the cache
must never serve stale orientation for a dirty tree.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL = REPO_ROOT / ".jspace" / "control.py"
PY = sys.executable

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asha import project_map

CONFIDENCES = ("direct", "detected", "inferred")
TOP_KEYS = {"schema", "cache_key", "repo", "stack", "layout", "tooling",
            "entry_points", "hotspots", "generated_candidates", "warnings"}
REPO_KEYS = {"tree_hash", "commit", "branch", "is_dirty"}

GITIGNORE = (".jspace/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
             ".mypy_cache/\n.ruff_cache/\n")

PYPROJECT = """[project]
name = "fixture-app"
requires-python = ">=3.11"
dependencies = ["fastapi>=0.100", "httpx"]

[project.scripts]
fixturecli = "pkg.main:run"

[tool.ruff]
line-length = 88

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.mypy]
strict = false
"""

MAIN_PY = """from typing import TypedDict

from fastapi import FastAPI

app = FastAPI()


class Item(TypedDict):
    name: str


def run():
    return app


if __name__ == "__main__":
    run()
"""

BASELINE = {
    ".gitignore": GITIGNORE,
    "pyproject.toml": PYPROJECT,
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "pkg/__init__.py": "",
    "pkg/main.py": MAIN_PY,
    "docs.md": "# docs\n",
}


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


def _paths(section: list[dict]) -> list[str]:
    return [entry["path"] for entry in section]


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _tree(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()


# ---- 1. quick orientation ------------------------------------------------

def test_quick_orientation_facts(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = project_map.build(repo, "quick", use_cache=False)

    assert result["stack"]["language"] == {
        "value": "python", "source": "pyproject.toml",
        "confidence": "direct"}
    assert result["stack"]["framework"]["value"] == "fastapi"
    assert result["stack"]["framework"]["confidence"] == "direct"
    assert _paths(result["layout"]["source_roots"]) == ["pkg"]
    assert _paths(result["layout"]["test_roots"]) == ["tests"]
    assert _paths(result["layout"]["package_roots"]) == ["pkg"]
    assert result["layout"]["project_name"]["value"] == "repo"

    tooling = result["tooling"]
    assert tooling["lint"]["value"] == "ruff"
    assert tooling["lint"]["confidence"] == "direct"
    assert tooling["test_runner"]["value"] == "pytest"
    assert tooling["type_checker"]["value"] == "mypy"
    configs = _paths(tooling["config_files"])
    assert configs == ["pyproject.toml"], configs

    # quick = cheap facts only; standard-only sections stay empty.
    assert result["entry_points"] == []
    assert result["hotspots"] == []
    assert result["generated_candidates"] == []
    assert result["warnings"] == []


# ---- 2. standard orientation --------------------------------------------

def test_standard_orientation_hotspots_and_entry_points(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = project_map.build(repo, "standard", use_cache=False)

    kinds = {entry["kind"] for entry in result["entry_points"]}
    assert "console_script" in kinds, "pyproject [project.scripts] is direct"
    assert "fastapi_app" in kinds, "`app = FastAPI()` must be detected"
    assert "cli_main" in kinds, "`if __name__ == '__main__'` must be detected"

    signals = {h["signal"] for h in result["hotspots"]}
    assert signals == {"revisions_seen", "unique_authors", "last_change",
                       "most_touched_recently"}
    by_signal = {h["signal"]: h for h in result["hotspots"]}
    assert by_signal["revisions_seen"]["value"] >= 1
    assert by_signal["unique_authors"]["value"] == 1
    assert by_signal["last_change"]["value"]["commit"] == _head(repo)
    touched = [i["path"] for i in by_signal["most_touched_recently"]["value"]]
    assert "pkg/main.py" in touched


# ---- 3. provenance -------------------------------------------------------

def test_provenance_every_fact(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = project_map.build(repo, "standard", use_cache=False)

    def check(item: dict, where: str) -> None:
        assert isinstance(item.get("source"), str) and item["source"], where
        assert item.get("confidence") in CONFIDENCES, (where, item)

    for key in ("language", "framework", "build_system"):
        value = result["stack"][key]
        if value is not None:
            check(value, "stack." + key)
    for key in ("test_runner", "lint", "type_checker"):
        value = result["tooling"][key]
        if value is not None:
            check(value, "tooling." + key)
    check(result["layout"]["project_name"], "layout.project_name")
    for key in ("source_roots", "test_roots", "package_roots",
                "important_dirs", "config_files"):
        section = (result["layout"].get(key)
                   or result["tooling"].get(key) or [])
        for entry in section:
            assert "path" in entry, (key, entry)
            check(entry, key)
    for key in ("entry_points", "hotspots", "generated_candidates",
                "warnings"):
        for entry in result[key]:
            check(entry, key)


# ---- 4. generated candidates --------------------------------------------

def test_generated_candidate(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "gen/defs.py",
           "# Code generated by mockgen. DO NOT EDIT.\nVALUE = 1\n")
    _commit_all(repo, "add generated file")
    result = project_map.build(repo, "standard", use_cache=False)

    paths = _paths(result["generated_candidates"])
    assert "gen/defs.py" in paths, paths
    entry = next(c for c in result["generated_candidates"]
                 if c["path"] == "gen/defs.py")
    assert entry["confidence"] == "detected"
    assert entry["source"] in ("file-header", "filename-pattern")
    # candidates, not asserted truth:
    assert "generated_files" not in result


# ---- 5. dirty-tree behavior ---------------------------------------------

def test_dirty_tree_bypasses_cache(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    first = project_map.build(repo, "standard", use_cache=True)
    assert first["repo"]["is_dirty"] is False
    assert first["cache_key"] == _tree(repo)
    cache_file = repo / ".jspace" / "cache" / "orient.json"
    assert cache_file.is_file(), "clean build must persist the cache"

    _write(repo, "pkg/main.py", MAIN_PY + "\n# dirty edit\n")
    second = project_map.build(repo, "standard", use_cache=True)
    assert second["cache_key"] is None, "dirty tree must bypass the cache"
    assert second["repo"]["is_dirty"] is True, (
        "stale clean-tree orientation must never be served")


# ---- 6. cache invalidation ----------------------------------------------

def test_cache_invalidation_on_head_change(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    first = project_map.build(repo, "standard", use_cache=True)
    assert first["cache_key"] == _tree(repo)

    _write(repo, "docs.md", "# docs\n\nmore\n")
    _commit_all(repo, "docs change")
    second = project_map.build(repo, "standard", use_cache=True)
    assert second["cache_key"] == _tree(repo)
    assert second["cache_key"] != first["cache_key"], (
        "cache identity is tree_hash: new tree must change it")
    assert second["repo"]["commit"] == _head(repo)
    by_signal = {h["signal"]: h for h in second["hotspots"]}
    assert by_signal["last_change"]["value"]["subject"] == "docs change", (
        "must be freshly computed, not the stale cached orientation")


# ---- 7. untracked file behavior -----------------------------------------

def test_untracked_file_not_stale_clean(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    project_map.build(repo, "standard", use_cache=True)
    _write(repo, "scratch.py", "X = 1\n")  # untracked, never committed
    result = project_map.build(repo, "standard", use_cache=True)
    assert result["repo"]["is_dirty"] is True, (
        "an untracked file must surface as a dirty tree")
    assert result["cache_key"] is None


# ---- 8. JSON schema ------------------------------------------------------

def test_json_schema(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    for mode in ("quick", "standard", "deep"):
        result = project_map.build(repo, mode, use_cache=False)
        assert set(result) == TOP_KEYS, (mode, set(result))
        assert result["schema"] == 1
        assert set(result["repo"]) == REPO_KEYS
        assert isinstance(result["entry_points"], list)
    deep = project_map.build(repo, "deep", use_cache=False)
    kinds = {entry["kind"] for entry in deep["entry_points"]}
    assert "schema_model" in kinds, "deep mode: TypedDict candidates"
    assert "public_symbols" in kinds, "deep mode: public symbol inventory"


# ---- 9. markdown rendering ----------------------------------------------

def test_markdown_rendering(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = project_map.build(repo, "standard", use_cache=False)
    markdown = project_map.render_markdown(result)
    for heading in ("Project:", "Language:", "Framework:", "Tests:",
                    "Lint:", "Type checking:", "Source roots:",
                    "Test roots:", "Entry points:", "Recent hotspots:",
                    "Generated candidates:", "Warnings:"):
        assert heading in markdown, heading
    assert "Project: repo" in markdown
    assert "pytest" in markdown and "ruff" in markdown
    # machine-readable JSON still valid via the CLI contract
    proc = subprocess.run(
        [PY, str(project_map.__file__), "--quick", "--format", "json",
         "--root", str(repo), "--no-cache"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert set(json.loads(proc.stdout)) == TOP_KEYS


# ---- orient wrapper (control.py) ----------------------------------------

def test_control_orient_wrapper(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = subprocess.run(
        [PY, str(CONTROL), "--transport", "local", "--root", str(repo),
         "orient", "--mode", "quick", "--format", "markdown"],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "Project:" in proc.stdout and "Language:" in proc.stdout
    # ledger-free: no control.json may be created by orient
    assert not (repo / ".jspace" / "control.json").exists()
    # fail-closed transport gate still applies to orient
    proc = subprocess.run(
        [PY, str(CONTROL), "--root", str(repo), "orient"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1
    assert "TRANSPORT GATE" in proc.stderr
