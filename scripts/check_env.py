#!/usr/bin/env python3
"""Environment parity checker for Asha (J.1).

Offline, non-mutating, deterministic. Validates that the current
environment satisfies a selected feature profile and reports actionable
missing dependencies.

Profiles:
  core  -- stdlib-only runtime + pytest (no feature extras required)
  full  -- core + code-search + memory extras (the CI verify profile)

Exit code: 0 if the profile is satisfiable, 1 otherwise.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# feature -> importable module name(s). Importability is the contract
# check: these are all lazy-imported at runtime, so "installed" ==
# "importable by the module that needs it".
FEATURES: dict[str, dict[str, list[str]]] = {
    "code-search": {
        "tree-sitter": ["tree_sitter"],
        "tree-sitter-languages": ["tree_sitter_languages"],
        "ast-grep-py": ["ast_grep_py"],
    },
    "memory": {
        "mem0ai": ["mem0"],
        "chromadb": ["chromadb"],  # mem0's embedded store
    },
}

REQUIRED_BASE = {
    "python": sys.version_info,
    "pytest": "pytest",
}

PROFILES = {
    "core": ("test", []),
    "full": ("test", ["code-search", "memory"]),
}


def _importable(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def main(argv: list[str]) -> int:
    profile = argv[1] if len(argv) > 1 else "core"
    if profile not in PROFILES:
        sys.stderr.write(
            f"check_env: unknown profile {profile!r} "
            f"(choose from: {', '.join(sorted(PROFILES))})\n"
        )
        return 2
    tool_name, features = PROFILES[profile]

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"python: {python_version}")
    print(f"profile: {profile}")
    print(f"packaging root: {Path(__file__).resolve().parents[1]}")

    missing: list[str] = []

    # Required base tooling
    for label, mod in REQUIRED_BASE.items():
        if label == "python":
            ok = sys.version_info >= (3, 11)
            print(f"  {'OK ' if ok else 'MISS'} {label}: {python_version} (need >=3.11)")
            if not ok:
                missing.append(label)
        else:
            ok = _importable(mod)
            print(f"  {'OK ' if ok else 'MISS'} {label}: {mod}")
            if not ok:
                missing.append(mod)

    # Feature deps
    for feature in features:
        for pkg, mods in FEATURES[feature].items():
            ok = all(_importable(m) for m in mods)
            print(f"  {'OK ' if ok else 'MISS'} [{feature}] {pkg}: {', '.join(mods)}")
            if not ok:
                missing.append(f"{feature}:{pkg}")

    if missing:
        print(f"\nenvironment unsuitable for profile {profile!r}")
        print("missing: " + ", ".join(missing))
        print("install hints:")
        if any(m.startswith("code-search") for m in missing):
            print("  pip install -e '.[code-search]'")
        if any(m.startswith("memory") for m in missing):
            print("  pip install -e '.[memory]'")
        if "pytest" in missing:
            print("  pip install -e '.[test]'")
        return 1

    print(f"\nenvironment suitable for {profile!r} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))