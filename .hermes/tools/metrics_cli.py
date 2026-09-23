"""CLI wiring over metrics + report_format (Asha self-upgrade).

`metrics` and `report_format` arrive from sibling workers, so they are
loaded dynamically inside the handler: the module stays importable (and
its tests green) in an isolated worktree where they do not exist yet,
and the bindings resolve in the union tree that `--apply` verifies."""
from __future__ import annotations

import argparse
import importlib
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Parser surface -- exists in every tree, needs no siblings."""
    parser = argparse.ArgumentParser(
        prog="metrics-cli", description="record and show in-process metrics")
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record", help="bump a counter")
    record.add_argument("name")
    sub.add_parser("show", help="render all counters")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    metrics = importlib.import_module("metrics")
    table = importlib.import_module("report_format")
    if args.command == "record":
        metrics.incr(args.name)
    print(table.format_table(metrics.snapshot()))
    return 0
