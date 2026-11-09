"""Optional read-only observability: real execution events + local dashboard.

Import surface used by the engine:

    from asha.observability import events as obs
    obs.start_run(root, run_id, meta)   # no-op unless ASHA_TELEMETRY is set
    obs.emit("worker.started", "dag", worker_id="w1", status="RUNNING")
    obs.finish_run("ok")

Nothing in this package participates in a governance decision: no sink,
no server and no reader can approve, reject or modify a run.
"""

from asha.observability import events, store

__all__ = ['events', 'store']
