"""Phase 5.2.5 -- demonstrated failure: a killed control process left a
stale ``.jspace/lock`` (O_EXCL file with the owner PID) and every
subsequent ledger operation was blocked forever.

Covers exactly that path: dead owner + old lock -> recovered; live
owner -> never touched (even when the lock is old)."""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

CONTROL_PY = Path(__file__).resolve().parents[1] / '.jspace' / 'control.py'


def _load_control():
    spec = importlib.util.spec_from_file_location('asha_control', CONTROL_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stale_lock_from_killed_process_is_recovered(tmp_path: Path) -> None:
    control = _load_control()
    lock = tmp_path / '.jspace' / 'lock'
    lock.parent.mkdir(parents=True)
    # a PID verified to have NO owning process right now (a freshly
    # reaped child PID is unreliable on Windows: the busy host reuses
    # it almost immediately, and _stale_lock must then stay
    # conservative and refuse to touch the lock)
    dead_pid = None
    for candidate in range(4194000, 4194020):
        try:
            os.kill(candidate, 0)
        except OSError:
            dead_pid = candidate
            break
    assert dead_pid is not None, 'no non-existent PID candidate found'
    lock.write_text(str(dead_pid), encoding='ascii')
    stale = time.time() - 60
    os.utime(lock, (stale, stale))

    with control.locked(tmp_path):        # must enter: owner is gone
        assert lock.is_file()             # held by US now
    assert not lock.is_file()             # released on exit


def test_live_owner_lock_is_never_recycled(tmp_path: Path) -> None:
    control = _load_control()
    lock = tmp_path / '.jspace' / 'lock'
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getpid()), encoding='ascii')   # WE are alive
    stale = time.time() - 60
    os.utime(lock, (stale, stale))
    try:
        raised = False
        with control.locked(tmp_path):    # 50 * 50ms -> ControlError
            pass
    except control.ControlError:
        raised = True
    assert raised                          # refused, not deleted
    assert lock.is_file()                  # live-owner lock preserved
    lock.unlink()


def test_fresh_lock_inside_retry_window_is_respected(tmp_path: Path) -> None:
    control = _load_control()
    lock = tmp_path / '.jspace' / 'lock'
    lock.parent.mkdir(parents=True)
    lock.write_text('19684', encoding='ascii')   # dead pid, but FRESH mtime
    raised = False
    try:
        with control.locked(tmp_path):
            pass
    except control.ControlError:
        raised = True
    assert raised                          # young lock never auto-removed
    assert lock.is_file()
    lock.unlink()
