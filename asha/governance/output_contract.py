"""What a worker must actually produce before a run may be called DONE.

A passing verification suite proves the tree still works; it does not prove the
worker did its job. A no-op worker leaves the workspace identical to the
baseline, passes every pre-existing test, and would otherwise be sealed as a
success. This module turns the task's declared expectation into a small,
auditable record that acceptance has to satisfy.

Kinds, declared with ``output_kind`` and derived from the worker's own fields
when omitted:

``changes``
    At least one file named in the declared ``writes`` changed. Temporary or
    unrelated files do not count, so noise cannot imitate work.
``artifact``
    Every path in ``required_artifacts`` exists, is non-empty, and was created
    or modified during this run. Read-only review tasks use this instead of
    touching tracked source.
``no_change``
    Nothing changed and ``no_change_reason`` says why that is correct.
``uncontracted``
    A legacy task that declared nothing. It keeps the ``changes`` rule over an
    empty write set, so it is never a success by silence: the task has to say
    what it meant to produce.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

KINDS = ("changes", "artifact", "no_change", "uncontracted")


def _paths(values: Iterable[Any] | None) -> list[str]:
    """Declared path list, normalised to forward slashes."""
    return [str(value).replace("\\", "/") for value in (values or [])]


def declared_kind(worker: dict[str, Any]) -> str:
    """The worker's contract kind: explicit, else derived from its own fields."""
    explicit = worker.get("output_kind")
    if explicit is not None:
        return str(explicit)
    if _paths(worker.get("required_artifacts")):
        return "artifact"
    if str(worker.get("no_change_reason") or "").strip():
        return "no_change"
    if _paths(worker.get("writes")):
        return "changes"
    return "uncontracted"


def _probe(root: Path | None, path: str, observed: set[str]) -> dict[str, Any]:
    """Facts about one required artifact: presence, size, digest, freshness."""
    record: dict[str, Any] = {"path": path, "exists": False, "size": None,
                              "sha256": None, "written_this_run": path in observed}
    if root is None:
        return record
    target = root / path
    if not target.is_file():
        return record
    data = target.read_bytes()
    record.update({"exists": True, "size": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()})
    return record


def evaluate(worker: dict[str, Any], *, observed: Iterable[str],
             writes_observed: Iterable[str],
             root: Path | None = None) -> dict[str, Any]:
    """Decide whether this run satisfied the task's output contract.

    ``observed`` is every path the run changed; ``writes_observed`` is the
    subset of the declared ``writes`` that actually changed. Nothing here
    inspects the worker's own claims: the answer comes from the tree.
    """
    kind = declared_kind(worker)
    seen = {str(item).replace("\\", "/") for item in observed}
    written = [str(item).replace("\\", "/") for item in writes_observed]
    artifacts = [_probe(root, path, seen)
                 for path in _paths(worker.get("required_artifacts"))
                 ] if kind == "artifact" else []
    record: dict[str, Any] = {
        "kind": kind, "satisfied": False, "reason": None,
        "declared_writes": _paths(worker.get("writes")),
        "writes_observed": written, "observed_count": len(seen),
        "artifacts": artifacts,
    }
    if kind not in KINDS:
        record["reason"] = f"unknown_output_kind:{kind}"
        return record
    if kind in ("changes", "uncontracted"):
        # A declared write must actually change. Without any declaration the
        # minimum is a real change somewhere in scope: work happened, but the
        # task never said which files it owned.
        record["satisfied"] = bool(written if kind == "changes" else seen)
        if not record["satisfied"]:
            record["reason"] = (
                "no_change_in_declared_writes"
                if kind == "changes" and record["declared_writes"]
                else "no_change_in_workspace")
        return record
    if kind == "no_change":
        reason = str(worker.get("no_change_reason") or "").strip()
        if not reason:
            record["reason"] = "no_change_without_reason"
        elif seen:
            record["reason"] = ("changed_files_under_no_change_contract:"
                                + ",".join(sorted(seen)[:5]))
        else:
            record["satisfied"] = True
        record["reason_text"] = reason
        return record
    missing = [item["path"] for item in artifacts
               if not item["exists"] or not item["size"]]
    stale = [item["path"] for item in artifacts
             if item["exists"] and item["size"] and not item["written_this_run"]]
    if missing:
        record["reason"] = "artifact_missing_or_empty:" + ",".join(missing[:5])
    elif stale:
        record["reason"] = "artifact_not_written_this_run:" + ",".join(stale[:5])
    else:
        record["satisfied"] = True
    return record
