"""Row rendering for metric snapshots (Asha self-upgrade).

Delivered by worker `summary_formatter`; deliberately independent of
`metrics_core` so both worktrees verify on their own -- the two only
meet in the union tree."""
from __future__ import annotations


def format_table(rows: dict[str, int]) -> str:
    """Deterministic `name=value` table, sorted by name."""
    if not rows:
        return "(no metrics)"
    return "\n".join(f"{name}={rows[name]}" for name in sorted(rows))
