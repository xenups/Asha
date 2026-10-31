"""Versioned, atomic review artifacts.

Location follows the existing external state convention
(``asha.common.paths.get_state_dir``), so a review record never lands in
the repository and can never be committed by accident::

    <state-root>/<checkout-id>/reviews/<run_id>/run.json
                                              target.json
                                              findings.json
                                              decision.json
                                              stages/<STAGE>.json
                                              stages/<STAGE>.packet.json

``run.json`` is the mutable state document (rewritten atomically on every
transition); stage files are written once and never replaced by a
different submission. Every write goes through a temp file and
``os.replace``, then is re-read and hashed -- a partially written or
tampered artifact is an error, never an empty run.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from asha.common import paths as common_paths
from asha.ladder.models import (
    ARTIFACT_SCHEMA,
    Confidence,
    Decision,
    Finding,
    Independence,
    ReviewerInfo,
    ReviewRun,
    ReviewTarget,
    RunNotFound,
    RunState,
    Severity,
    StageResult,
    StageStatus,
    StageType,
    Verdict,
)

RUN_FILE = 'run.json'
RUN_ID_PREFIX = 'rv-'
REQUIRED_KEYS = ('schema', 'artifact_schema', 'run_id', 'state', 'target',
                 'stages')


class ArtifactError(Exception):
    """A persisted review artifact is missing, malformed or tampered."""


def new_run_id() -> str:
    stamp = dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')
    return f'{RUN_ID_PREFIX}{stamp}-{secrets.token_hex(4)}'


def reviews_root(root: Path) -> Path:
    return common_paths.get_state_dir(Path(root).resolve()) / 'reviews'


def run_dir(root: Path, run_id: str) -> Path:
    if not run_id or '/' in run_id or '\\' in run_id or run_id.startswith('.'):
        raise ArtifactError(f'illegal review run id {run_id!r}')
    return reviews_root(root) / run_id


def stage_path(root: Path, run_id: str, stage: StageType, *,
               packet: bool = False) -> Path:
    name = f'{stage.value}.packet.json' if packet else f'{stage.value}.json'
    return run_dir(root, run_id) / 'stages' / name


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, document: dict[str, Any]) -> str:
    """Write one JSON document atomically; return its sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(document, indent=2, sort_keys=True,
                       ensure_ascii=False) + '\n').encode('utf-8')
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix='.tmp-')
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    readback = path.read_bytes()
    if _digest(readback) != _digest(body):
        raise ArtifactError(f'artifact {path} did not round-trip')
    return _digest(body)


def save_run(root: Path, run: ReviewRun) -> str:
    """Persist the run state; returns the run directory."""
    directory = run_dir(root, run.run_id)
    _atomic_write(directory / RUN_FILE, run.as_dict())
    _atomic_write(directory / 'target.json', run.target.as_dict())
    _atomic_write(directory / 'findings.json', {
        'schema': run.as_dict()['schema'],
        'run_id': run.run_id,
        'independence': run.independence.value,
        'findings': [finding.as_dict() for finding in run.findings],
        'relations': list(run.relations),
        'semantic_validation': run.semantic_validation,
    })
    if run.decision is not None:
        _atomic_write(directory / 'decision.json', {
            'run_id': run.run_id,
            'state': run.state.value,
            'decision': run.decision.value,
            'approved': run.approved,
            'reasons': list(run.reasons),
            'independence': run.independence.value,
            'execution_mode': run.execution_mode,
            'gate': run.gate,
            'finalized_at': run.finalized_at,
        })
    for record in run.stages:
        _atomic_write(stage_path(root, run.run_id, record.stage),
                      record.as_dict())
    return str(directory)


def write_packet(root: Path, run_id: str, stage: StageType,
                 document: dict[str, Any]) -> str:
    """Persist the packet identity a stage was answered against."""
    return _atomic_write(stage_path(root, run_id, stage, packet=True),
                         document)


def load_run(root: Path, run_id: str) -> ReviewRun:
    """Re-read a persisted run; missing or malformed data is an error."""
    path = run_dir(root, run_id) / RUN_FILE
    if not path.is_file():
        raise RunNotFound(
            f'no review run {run_id!r} under {reviews_root(root)}')
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f'unreadable review artifact {path}: {exc}') from exc
    if not isinstance(document, dict):
        raise ArtifactError(f'review artifact {path} is not an object')
    missing = [key for key in REQUIRED_KEYS if key not in document]
    if missing:
        raise ArtifactError(
            f'review artifact {path} is incomplete: missing '
            + ', '.join(sorted(missing)))
    if document.get('artifact_schema') != ARTIFACT_SCHEMA:
        raise ArtifactError(
            f'review artifact {path} has schema '
            f'{document.get("artifact_schema")!r}, expected {ARTIFACT_SCHEMA}')
    try:
        target = ReviewTarget(**{
            key: value for key, value in document['target'].items()
            if key in ReviewTarget.__dataclass_fields__})
        stages = tuple(_stage_from(item) for item in document['stages'])
        findings = tuple(_finding_from(item) for item in document['findings'])
        decision = document.get('decision')
        return ReviewRun(
            run_id=str(document['run_id']),
            target=target,
            state=RunState(document['state']),
            stages=stages,
            decision=Decision(decision) if decision else None,
            reasons=tuple(str(reason) for reason in document.get('reasons', [])),
            findings=findings,
            relations=tuple(document.get('relations', [])),
            independence=Independence(document.get('independence', 'UNKNOWN')),
            semantic_validation=dict(document.get('semantic_validation') or {}),
            gate=dict(document.get('gate') or {}),
            artifacts=str(document.get('artifacts') or ''),
            execution_mode=str(document.get('execution_mode') or ''),
            created_at=str(document.get('created_at') or ''),
            expires_at=str(document.get('expires_at') or ''),
            finalized_at=str(document.get('finalized_at') or ''),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError(
            f'review artifact {path} does not satisfy the schema: {exc}') from exc


def _stage_from(item: Any) -> StageResult:
    if not isinstance(item, dict):
        raise ArtifactError('stage entry is not an object')
    reviewer = item.get('reviewer') or {}
    return StageResult(
        stage=StageType(item['stage']),
        status=StageStatus(item['status']),
        stage_id=str(item.get('stage_id', '')),
        verdict=Verdict(item.get('verdict', 'NOT_APPLICABLE')),
        summary=str(item.get('summary', '')),
        reviewer=ReviewerInfo(
            host_client=str(reviewer.get('host_client', 'unknown')),
            isolation=Independence(
                reviewer.get('isolation_claim', 'UNKNOWN')),
            attestation_source=str(reviewer.get('attestation_source', '')),
        ),
        execution_id=str(item.get('execution_id', '')),
        submission_sha256=str(item.get('submission_sha256', '')),
        submitted_at=str(item.get('submitted_at', '')),
        validated_at=str(item.get('validated_at', '')),
        findings=tuple(_finding_from(entry)
                       for entry in item.get('findings') or []),
        error=str(item.get('error', '')),
        artifact=str(item.get('artifact', '')),
    )


def _finding_from(item: Any) -> Finding:
    if not isinstance(item, dict):
        raise ArtifactError('finding entry is not an object')
    return Finding(
        finding_id=str(item['finding_id']),
        stage=StageType(item['stage']),
        category=str(item['category']),
        severity=Severity(item['severity']),
        confidence=Confidence(item['confidence']),
        title=str(item['title']),
        description=str(item['description']),
        file_path=str(item['file_path']),
        line_start=item.get('line_start'),
        line_end=item.get('line_end'),
        evidence=str(item.get('evidence', '')),
        impact=str(item.get('impact', '')),
        recommended_action=str(item.get('recommended_action', '')),
        blocking=bool(item.get('blocking', False)),
    )


def list_runs(root: Path) -> list[str]:
    """Run ids of every persisted review, newest last (name order)."""
    directory = reviews_root(root)
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir()
                  if (path / RUN_FILE).is_file())
