#!/usr/bin/env python3
"""Turn the frozen Phase 10 measurements into a table and a machine record.

Input : results.json (hand-curated from verified logs and independent probes)
Output: results.md (markdown table), stdout summary
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE / "results.json").read_text(encoding="utf-8"))

COLUMNS = ["task", "condition", "wall_clock_s", "model_calls", "commit",
           "tests_passed", "acceptance_met", "gate_state",
           "authorized_to_ship", "out_of_scope_paths"]


def main() -> int:
    rows = DATA["pairs"]
    lines = ["| " + " | ".join(COLUMNS) + " |",
             "|" + "|".join(["---"] * len(COLUMNS)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in COLUMNS) + " |")
    table = "\n".join(lines) + "\n"
    (HERE / "results_table.md").write_text(table, encoding="utf-8")

    needed = ["T1", "T2", "T3"]
    have = {c: {r["task"] for r in rows if r["condition"] == c}
            for c in ("baseline", "asha")}
    complete = all(have[c] >= set(needed) for c in have)
    acceptance = all(r["acceptance_met"] == "yes" for r in rows)
    authorised = [r for r in rows if r["authorized_to_ship"] == "yes"]
    print(table)
    print(json.dumps({
        "pairs": len(rows) // 2,
        "all_pairs_complete": complete,
        "every_run_met_acceptance": acceptance,
        "runs_authorised_to_ship": len(authorised),
        "asha_runs_authorised": len([r for r in authorised
                                     if r["condition"] == "asha"]),
        "baseline_runs_authorised": len([r for r in authorised
                                         if r["condition"] == "baseline"]),
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
