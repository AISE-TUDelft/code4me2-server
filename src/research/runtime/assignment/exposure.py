"""Idempotent exposure receipts.

An exposure is recorded once an agent handshake actually starts (or a launch
fails). Receipts are idempotent by key: a replay preserves the first terminal
result and audits the retry, and a launch failure is stored as a non-exposure
with evidence. Recording an exposure never rewrites the assignment.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from .enums import ExposureOutcome, ExposureReasonCode
from .models import (
    AssignmentV1,
    ExposureEnvironment,
    ExposureIssue,
    ExposureResult,
    ExposureV1,
)


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _rejected(
    code: ExposureReasonCode, message: str, field: str = ""
) -> ExposureResult:
    return ExposureResult(
        accepted=False,
        reason=code,
        issue=ExposureIssue(code=code, message=message, field=field),
    )


def record_exposure(
    assignment: AssignmentV1,
    environment: ExposureEnvironment,
    outcome: ExposureOutcome,
    idempotency_key: str,
    *,
    existing: Optional[ExposureV1] = None,
    agent_release_id: Optional[str] = None,
    artifact_digest: Optional[str] = None,
    adapter_version: Optional[str] = None,
    observed_configuration: Optional[dict] = None,
    evidence_digest: Optional[str] = None,
    now: Optional[datetime] = None,
    kill_switch_check: Optional[Callable[[], bool]] = None,
) -> ExposureResult:
    """Record (or idempotently replay) one exposure receipt for an assignment."""
    timestamp = _now(now)

    # Operator kill switch: no exposure may be written while engaged. The check
    # is injected so this service keeps no operations dependency.
    if kill_switch_check is not None and kill_switch_check():
        return _rejected(
            ExposureReasonCode.KILL_SWITCH_ENGAGED,
            "an operator kill switch is engaged for this scope",
            "kill_switch",
        )

    if assignment is None:
        return _rejected(
            ExposureReasonCode.ASSIGNMENT_MISMATCH,
            "an assignment is required to record an exposure",
            "assignment_id",
        )

    if not idempotency_key or not idempotency_key.strip():
        return _rejected(
            ExposureReasonCode.IDEMPOTENCY_KEY_REQUIRED,
            "an idempotency key is required for an exposure receipt",
            "idempotency_key",
        )

    if existing is not None:
        if existing.idempotency_key == idempotency_key:
            return ExposureResult(
                accepted=True,
                exposure=existing,
                reused=True,
                is_exposure=existing.is_exposure,
                reason=ExposureReasonCode.IDEMPOTENT_REPLAY,
                audit=[
                    "idempotent replay; first terminal outcome preserved "
                    f"({existing.outcome.value})"
                ],
            )
        return _rejected(
            ExposureReasonCode.CONFLICT,
            "an exposure is already recorded for this assignment under a different key",
            "idempotency_key",
        )

    exposure = ExposureV1(
        exposure_id=uuid.uuid4(),
        assignment_id=assignment.assignment_id,
        study_id=assignment.study_id,
        environment=environment,
        agent_release_id=agent_release_id or "",
        artifact_digest=artifact_digest,
        adapter_version=adapter_version,
        observed_configuration=dict(observed_configuration or {}),
        started_at=timestamp,
        outcome=outcome,
        evidence_digest=evidence_digest,
        idempotency_key=idempotency_key,
        created_at=timestamp,
    )
    return ExposureResult(
        accepted=True,
        exposure=exposure,
        reused=False,
        is_exposure=exposure.is_exposure,
        reason=ExposureReasonCode.OK,
    )
