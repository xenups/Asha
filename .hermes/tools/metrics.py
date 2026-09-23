"""Backward-compatibility shim: re-exports `asha.metrics` in place of the
pre-refactor module (Phase refactor: orchestrator -> top-level asha/)."""
from __future__ import annotations

import importlib as _importlib
import sys as _sys
from pathlib import Path as _Path

for _root in _Path(__file__).resolve().parents:
    if (_root / "pyproject.toml").is_file():
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        break
# Static surface first: mypy and ruff resolve this star-forwarder, while
# the attribute copy below supplies runtime privates (star skips _names).
from asha.metrics import *

_real = _importlib.import_module("asha.metrics")
globals().update({k: v for k, v in vars(_real).items()
                  if not k.startswith("__")})
if getattr(_real, "__all__", None):
    __all__ = list(_real.__all__)

# Direct-script execution (`python .hermes/tools/<name>.py --args`) must
# keep the original CLI semantics: several of these tools are launched as
# scripts by tests and by control.py, and a silent import-only shim would
# drop their __main__ block (observed: test_memory asserting `scope=`).
if __name__ == "__main__":
    import runpy as _runpy

    _runpy.run_module("asha.metrics", run_name="__main__", alter_sys=True)
