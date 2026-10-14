#!/usr/bin/env python3
"""Linux (or any) node smoke test for Asha.

Three checks, all offline except (3):

1. snapshot hashing   -- audit_snapshot.snapshot_id_of() on this checkout
2. gate evaluation    -- phase2 evidence bound to the REAL snapshot id:
                         healthy path must authorize, a WRONG snapshot id
                         must be denied (fail-closed proof)
3. live LLM           -- resolve_live_reviewer(): SKIPPED unless
                         ASHA_SEMANTIC_REVIEW=live and ASHA_LLM_API_KEY are
                         set; when enabled, one real bounded request

Exit codes: 0 = every executed check passed (live may be SKIPPED-by-config),
1 = any executed check failed (never reported as a pass).
"""
from __future__ import annotations

import sys
from pathlib import Path

from asha import audit_snapshot, evidence, gate_states
from asha.governance import live_reviewer, ship_gate

ROOT = Path(__file__).resolve().parents[1]


def _payload(snapshot_id: str) -> dict:
    """Minimal phase2 evidence: all required checks passed, clean review."""
    return {
        "snapshot_id": snapshot_id,
        "evidence_schema": evidence.SCHEMA,
        "policy_version": "phase2",
        "required_checks": ["ruff", "pytest", "mypy"],
        "checks": [
            {"name": name, "status": "passed", "exit_code": 0}
            for name in ("ruff", "pytest", "mypy")
        ],
        "findings": [],
        "semantic_review": {
            "schema": "semantic-review/v1",
            "snapshot_id": snapshot_id,
            "policy_version": ship_gate.POLICY_VERSION,
            "status": "ok",
            "findings": [],
        },
    }


def main() -> int:
    failures = 0

    # 1. snapshot hashing -----------------------------------------------
    snapshot_id = audit_snapshot.snapshot_id_of(ROOT)
    print(f"[1/3] snapshot_id_of(root) = {snapshot_id}")
    if len(snapshot_id) != 64 or snapshot_id == "0" * 64:
        print("      FAIL: not a sha256 id")
        failures += 1

    # 2. gate evaluation -------------------------------------------------
    healthy = gate_states.evaluate(
        gate_states.validate_evidence(_payload(snapshot_id)),
        authorized_snapshot_id=snapshot_id)
    ok_healthy = bool(healthy.authorized_to_ship)
    print(f"[2/3] healthy evidence -> authorized_to_ship={ok_healthy}")

    tampered = gate_states.evaluate(
        gate_states.validate_evidence(_payload("f" * 64)),
        authorized_snapshot_id=snapshot_id)
    ok_failclosed = not bool(tampered.authorized_to_ship)
    print(f"[2/3] wrong snapshot   -> denied={ok_failclosed} "
          f"(state={getattr(tampered, 'state', '?')})")
    if not ok_healthy:
        print("      FAIL: healthy evidence denied")
        failures += 1
    if not ok_failclosed:
        print("      FAIL: tampered evidence NOT denied (violates fail-closed)")
        failures += 1

    # 3. live LLM connectivity -------------------------------------------
    reviewer = live_reviewer.resolve_live_reviewer()
    if reviewer is None:
        print("[3/3] live LLM        -> SKIPPED (set ASHA_SEMANTIC_REVIEW=live"
              " and ASHA_LLM_API_KEY to enable; SKIPPED is not a PASS)")
    else:
        try:
            artifact = live_reviewer.run_live_semantic_reviewer(
                ROOT, snapshot_id, 120)
            print(f"[3/3] live LLM        -> OK: status={artifact.get('status')!r}"
                  f" findings={len(artifact.get('findings', []))}"
                  f" echo_ok={artifact.get('snapshot_id') == snapshot_id}")
            if artifact.get("snapshot_id") != snapshot_id:
                failures += 1
        except Exception as exc:  # ReviewerError and transport failures
            print(f"[3/3] live LLM        -> FAIL: {exc}")
            failures += 1

    print("SMOKE:", "OK" if failures == 0 else f"FAILED ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
