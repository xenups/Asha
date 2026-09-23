"""Direct package execution: `python orchestrator/` or
`python -m orchestrator`."""
import sys

if __package__ in (None, ''):
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from orchestrator.scheduler import main
else:
    from .scheduler import main

raise SystemExit(main(sys.argv[1:]))
