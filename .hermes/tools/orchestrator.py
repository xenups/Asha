#!/usr/bin/env python3
"""Backward-compatible script entry for the `orchestrator` package.

control.py and tests execute this exact path; the former monolith
lives in orchestrator/{types,conflict,worktree,scheduler}.py with
`orchestrator/__init__.py` as the public facade. `import orchestrator`
resolves the PACKAGE (package precedence over the same-named file),
so every import keeps its old target."""
import sys

from orchestrator.scheduler import main

if __name__ == '__main__':
    sys.exit(main())
