"""Ship-gate surface (extracted from the deleted .jspace/control.py, H.1).

Preserves the EXACT gate semantics that control.py `check --stage ship`
executed, factored onto the kept modules (evidence, check_runner,
scope_resolver, scoping) — no scheduler, no AST, no in-tree state.

Pre-D restoration (from Phase D.1): when the base is unresolved and the
affected-file set is empty, the full S2 equivalent suite (ruff, pytest,
mypy) runs instead of defaulting to the S0 subset, so committed runtime
files never skip lint silently (the legacy HEAD~1 fallback's effect).
"""

from __future__ import annotations

import json
from pathlib import Path

from asha import check_runner, evidence, scope_resolver

def gate_ship(root: Path, *, no_execute: bool = False) -> dict:
    """Run the ship gate exactly as control.py did; returns the sealed
    evidence payload. Raises evidence.EvidenceError on refusal."""
    root = Path(root).resolve()
    # tamper check on any prior artifact BEFORE anything else
    evidence.verify(root)
    evidence.require_clean_tree(root)
    resolved = scope_resolver.resolve(root)
    # Pre-D restoration (D.1): unresolved base + empty diff -> full S2
    if resolved["base"] is None and not resolved["affected_files"]:
        resolved["checks"] = ["ruff", "pytest", "mypy"]
    if no_execute:
        checks: list[dict] = []
    else:
        checks = check_runner.run(root, resolved)
    ok = all(c["status"] in ("passed", "skipped") for c in checks)
    sealed = evidence.seal({
        "schema": evidence.SCHEMA,
        "stage": "ship",
        "scope": resolved["scope"],
        "commit": evidence.head_hash(root),
        "tree_hash": evidence.tree_hash(root),
        "observed_at": evidence.now_iso(),
        "checks": checks,
        "authorized_to_ship": ok,
    })
    evidence.write(root, sealed)  # persist BEFORE refusal, as control.py did
    evidence.verify(root)  # roundtrip self-check
    if not ok:
        raise evidence.EvidenceError(
            "SHIP GATE REFUSED: failing checks "
            + ", ".join(c["name"] for c in checks if c["status"] != "passed"))
    from asha.common import paths as common_paths
    ev_file = common_paths.get_evidence_dir(root) / "evidence.json"
    return json.loads(ev_file.read_text(encoding="utf-8"))


def orient_quick(root: Path) -> dict:
    """Orient the repository (project_map quick mode) — the surface the
    legacy `control.py orient` exposed."""
    root = Path(root).resolve()
    from asha import project_map
    return project_map.build(root, "quick", use_cache=True)


def schema_version() -> int:
    """The evidence schema version the gate seals at."""
    return evidence.SCHEMA