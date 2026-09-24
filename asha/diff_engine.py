#!/usr/bin/env python3
"""Aider-style atomic SEARCH/REPLACE diff engine.

Strict block parser:
    <<<<<<< SEARCH
    [verbatim lines]
    =======
    [replacement lines]
    >>>>>>> REPLACE

Contract:
- SEARCH block must match the target file EXACTLY (100% char match).
- ValueError if the SEARCH block is missing or occurs more than once.
- Atomic write: patched content goes to a temp sibling file first,
  integrity-verified, then atomically replaces the target (os.replace).

Local disk only. No remote/SSH patching.
"""

from __future__ import annotations

import sys

if __package__ in (None, ""):
    # Direct-script mode: drop this directory from sys.path BEFORE any
    # stdlib import -- the sibling types.py would otherwise shadow stdlib
    # `types` and kill the import chain on GenericAlias (same guard as
    # asha/__main__.py and asha/mcp_server.py).
    def _asha_norm(entry: str) -> str:
        return (entry or ".").replace(chr(92), "/").rstrip("/").lower()

    _asha_pkg = _asha_norm(__file__).rsplit("/", 1)[0]
    sys.path = [entry for entry in sys.path if _asha_norm(entry) != _asha_pkg]
    sys.path.insert(0, _asha_pkg.rsplit("/", 1)[0])

import argparse
import os
import tempfile
from pathlib import Path

_SEARCH = "<<<<<<< SEARCH"
_MID = "======="
_REPLACE = ">>>>>>> REPLACE"


def parse_patch(patch_text: str) -> list[tuple[str, str]]:
    """Parse patch text into [(search_block, replace_block), ...].

    Returns list so one file can carry multiple hunks; each hunk's search
    block is stripped of trailing newline before matching.
    """
    hunks: list[tuple[str, str]] = []
    lines = patch_text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == _SEARCH:
            search: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != _MID:
                search.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ValueError("malformed hunk: missing '=======' divider")
            i += 1  # consume divider
            replace: list[str] = []
            while i < len(lines) and lines[i].strip() != _REPLACE:
                replace.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ValueError("malformed hunk: missing '>>>>>>> REPLACE' terminator")
            i += 1  # consume terminator
            hunks.append(("\n".join(search), "\n".join(replace)))
        else:
            i += 1
    if not hunks:
        raise ValueError("no SEARCH/REPLACE block found in patch text")
    return hunks


def apply_patch(target_path: str | os.PathLike, patch_text: str) -> bool:
    """Apply all hunks to a local file, atomically.

    Returns True if any hunk was applied, False if all were no-ops
    (search block matched nothing -> content unchanged).
    Raises ValueError on malformed patch or non-unique/missing match.
    """
    target = Path(target_path)
    if not target.is_file():
        raise FileNotFoundError(f"target file not found: {target}")

    original = target.read_text(encoding="utf-8")
    patched = original
    applied_any = False

    for search_block, replace_block in parse_patch(patch_text):
        if search_block not in patched:
            raise ValueError(
                f"SEARCH block not found in {target}:\n{search_block!r}"
            )
        if patched.count(search_block) > 1:
            raise ValueError(
                f"SEARCH block is ambiguous (occurs {patched.count(search_block)} times) "
                f"in {target}; refusing to guess:\n{search_block!r}"
            )
        patched = patched.replace(search_block, replace_block, 1)
        applied_any = True

    if not applied_any:
        return False

    # Atomicity: write temp sibling, verify, then os.replace.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.stem + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(patched)
        tmp = Path(tmp_name)
        if tmp.read_text(encoding="utf-8") != patched:
            raise RuntimeError("integrity check failed: temp file mismatch")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True


# ---- CLI ---------------------------------------------------------------

def _cli() -> None:
    ap = argparse.ArgumentParser(description="Atomic SEARCH/REPLACE patch tool (local files only).")
    ap.add_argument("--file", required=True, help="target file to patch")
    ap.add_argument("--patch", required=True, help="patch text (SEARCH/REPLACE blocks)")
    args = ap.parse_args()
    applied = apply_patch(args.file, args.patch)
    print(f"OK: {'patched' if applied else 'no-op'} {os.path.abspath(args.file)}")
    sys.exit(0)


# ---- Self-test ---------------------------------------------------------

def self_test() -> None:
    """End-to-end: temp file -> dummy patch -> assert -> cleanup."""
    tmp_dir = Path(tempfile.gettempdir())
    victim = tmp_dir / "diff_engine_selftest_target.txt"
    victim.write_text("line one\nneedle here\nline three\n", encoding="utf-8")

    patch = (
        "<<<<<<< SEARCH\n"
        "needle here\n"
        "=======\n"
        "replaced needle\n"
        ">>>>>>> REPLACE\n"
    )
    apply_patch(victim, patch)
    content = victim.read_text(encoding="utf-8")
    assert content == "line one\nreplaced needle\nline three\n", content

    # Failure cases must raise ValueError.
    def _expect_valueerror(fn):
        try:
            fn()
        except ValueError:
            return
        raise AssertionError("expected ValueError")

    _expect_valueerror(
        lambda: apply_patch(victim, "<<<<<<< SEARCH\nabsent line\n=======\nx\n>>>>>>> REPLACE\n")
    )
    _expect_valueerror(
        lambda: apply_patch(
            victim,
            "<<<<<<< SEARCH\nreplaced needle\n=======\nx\n>>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\nreplaced needle\n=======\ny\n>>>>>>> REPLACE\n",
        )
    )

    victim.unlink()
    assert not victim.exists()
    print("diff_engine self-test PASSED (patch applied, ValueError on miss & ambiguity, cleanup ok)")
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    else:
        _cli()