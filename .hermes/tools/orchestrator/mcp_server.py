"""Backward-compatibility shim for asha.mcp_server: keeps the proven
direct-script spawn form (the hermes registration runs
`python .hermes/tools/orchestrator/mcp_server.py`)."""
from __future__ import annotations

import sys

if __package__ in (None, ""):
    # Direct-script spawn: drop the package dir from sys.path BEFORE any
    # stdlib import -- otherwise the shim dir's `types.py` shadows stdlib
    # `types` and `import json` dies in the enum chain (circular). Pure
    # string ops only: pathlib/json are unsafe while the shadow sits on
    # the path. Same proven guard as the pre-refactor module.
    import sys as _script_sys

    def _norm(entry: str) -> str:
        return (entry or ".").replace(chr(92), "/").rstrip("/").lower()

    _pkg_dir = _norm(__file__).rsplit("/", 1)[0]
    _script_sys.path = [entry for entry in _script_sys.path
                        if _norm(entry) != _pkg_dir]
    _script_sys.path.insert(0, _pkg_dir.rsplit("/", 1)[0])
    __package__ = "orchestrator"

# Safe after the filter above: the package dir no longer precedes stdlib
# `types` on sys.path, so pathlib/importlib are legal now.
import importlib as _importlib
from pathlib import Path as _Path

for _root in _Path(__file__).resolve().parents:
    if (_root / "pyproject.toml").is_file():
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        break
# Static surface: gives `main` a real binding for ruff F821 / mypy.
from asha.mcp_server import *

_real = _importlib.import_module("asha.mcp_server")
globals().update({k: v for k, v in vars(_real).items()
                  if not k.startswith("__")})
if getattr(_real, "__all__", None):
    __all__ = list(_real.__all__)

if __name__ == "__main__":
    sys.exit(main())
