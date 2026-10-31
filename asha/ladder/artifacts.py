"""Versioned, atomic review artifacts.

Location follows the existing external state convention
(``asha.common.paths.get_state_dir``), so a review record never lands in
the repository and can never be committed by accident::

    <state-root>/<checkout-id>/reviews/<run_id>/run.json
                                              target.json
                                              findings.json
                                              decision.json
                                              stages/<STAGE>.json

``run.json`` is the record ``load_run`` re-reads; the per-stage files exist
so a human can read one stage without parsing the whole run. Every file is
written through a temp file and ``os.replace``, then re-read and hashed --
a partially written or tampered artifact is an error, never an empty run.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    ReviewRunResult,
    ReviewTarget,
    Severity,
    StageResult,
    StageStatus,
    StageType,
    Verdict,
)

RUN_FILE = 'run.json'
RUN_ID_PREFIX = 'rv-'
REQUIRED_KEYS = ('schema', 'artifact_schema', 'run_id', 'status', 'decision',
                 'target', 'stages', 'findings', 'gate')


class ArtifactError(Exception):
    """A persisted review artifact is missing, malformed or tampered."""


class ReviewNotFound(ArtifactError):
    """No run exists under that id."""


def reviews_root(root: Path) -> Path:
    return common_paths.get_state_dir(Path(root).resolve()) / 'reviews'


def run_dir(root: Path, run_id: str) -> Path:
    if not run_id or '/' in run_id or run_id.startswith('.'):
        raise ArtifactError(f'illegal review run id {run_id!r}')
    return reviews_root(root) / run_id


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


def write_run(root: Path, result: ReviewRunResult) -> str:
    """Persist one run; returns the run directory."""
    directory = run_dir(root, result.run_id)
    document = result.as_dict()
    document['artifact_schema'] = ARTIFACT_SCHEMA
    _atomic_write(directory / RUN_FILE, document)
    _atomic_write(directory / 'target.json', result.target.as_dict())
    _atomic_write(directory / 'findings.json', {
        'schema': result.as_dict().get('schema'),
        'run_id': result.run_id,
        'findings': [finding.as_dict() for finding in result.findings],
        'relations': list(result.relations),
    })
    _atomic_write(directory / 'decision.json', {
        'run_id': result.run_id,
        'status': result.status.value,
        'decision': result.decision.value,
        'approved': result.approved,
        'reasons': list(result.reasons),
        'independence': result.independence.value,
        'gate': result.gate,
    })
    for stage in result.stages:
        _atomic_write(directory / 'stages' / f'{stage.stage.value}.json',
                      stage.as_dict())
    return str(directory)


def load_run(root: Path, run_id: str) -> ReviewRunResult:
    """Re-read a persisted run; missing or malformed data is an error."""
    path = run_dir(root, run_id) / RUN_FILE
    if not path.is_file():
        raise ReviewNotFound(f'no review run {run_id!r} under {reviews_root(root)}')
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
        return ReviewRunResult(
            run_id=str(document['run_id']),
            target=target,
            status=StageStatus(document['status']),
            decision=Decision(document['decision']),
            reasons=tuple(str(reason) for reason in document.get('reasons', [])),
            stages=stages,
            findings=findings,
            relations=tuple(document.get('relations', [])),
            independence=Independence(document.get('independence', 'unknown')),
            gate=dict(document.get('gate') or {}),
            artifacts=str(document.get('artifacts') or ''),
            started_at=str(document.get('started_at') or ''),
            ended_at=str(document.get('ended_at') or ''),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError(
            f'review artifact {path} does not satisfy the schema: {exc}') from exc


def _stage_from(item: Any) -> StageResult:
    if not isinstance(item, dict):
        raise ArtifactError('stage entry is not an object')
    return StageResult(
        stage=StageType(item['stage']),
        status=StageStatus(item['status']),
        verdict=Verdict(item.get('verdict', 'NOT_APPLICABLE')),
        reviewer=str(item.get('reviewer', '')),
        execution_id=str(item.get('execution_id', '')),
        provider=str(item.get('provider', '')),
        model=str(item.get('model', '')),
        session_id=str(item.get('session_id', '')),
        prompt_sha256=str(item.get('prompt_sha256', '')),
        context_sha256=str(item.get('context_sha256', '')),
        started_at=str(item.get('started_at', '')),
        ended_at=str(item.get('ended_at', '')),
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
