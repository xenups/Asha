"""Backward-compatibility shim: re-exports the `asha` package
surface in place of the pre-refactor orchestrator package facade.
NOTE: the facade refers to the PACKAGE (`asha`), never `asha.__init__`
-- naming the __init__ file as its own module registers the source file
twice under different module names and mypy fails with "Source file found
twice" (measured)."""
from __future__ import annotations

import importlib as _importlib
import sys as _sys
from pathlib import Path as _Path

for _root in _Path(__file__).resolve().parents:
    if (_root / "pyproject.toml").is_file():
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        break
from asha import *

_real = _importlib.import_module("asha")
globals().update({k: v for k, v in vars(_real).items()
                  if not k.startswith("__")})
if getattr(_real, "__all__", None):
    __all__ = list(_real.__all__)


