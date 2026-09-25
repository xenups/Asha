"""Package entry: `python -m asha` / `asha` (Phase 5.2.2 CLI; the
legacy scheduler form `asha run --spec ...` passes through unchanged
via asha.cli). Originally: `python -m orchestrator` (or directly
`python orchestrator/__main__.py`).

`python orchestrator/` (directory form) is NOT supported: CPython
bootstraps runpy while the package dir itself is sys.path[0], so the
stdlib `types` import resolves as orchestrator/types.py before any
line of user code could intervene (the fixed module layout makes this
un-engineerable from inside)."""
import sys

if __package__ in (None, ''):
    # String-only filter BEFORE any stdlib import: even pathlib would
    # trigger fnmatch -> re -> enum -> `from types import ...` and hit
    # the package dir still sitting at sys.path[0].
    def _norm(entry: str) -> str:
        return (entry or '.').replace(chr(92), '/').rstrip('/').lower()

    pkg = _norm(__file__).rsplit('/', 1)[0]
    sys.path = [entry for entry in sys.path if _norm(entry) != pkg]
    sys.path.insert(0, pkg.rsplit('/', 1)[0])
    from asha.cli import main
else:
    from .cli import main

if __name__ == '__main__':
    # `python -m asha` executes this module; the console-script shim
    # imports `main` and calls it itself -- importing this module for
    # any other reason must never run the CLI or raise SystemExit.
    raise SystemExit(main(sys.argv[1:]))
