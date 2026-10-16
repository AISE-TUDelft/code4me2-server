"""Terminal-status retention behaviour (Task 06 §9).

Retention is an admin/compliance operation: it must understand the terminal
enrollment statuses ``STUDY_STOPPED`` and ``REVOKED`` (never treating a stopped
study as "cannot be retained"), keep audit evidence, and never be triggered by
an ordinary study stop. The in-memory store double mirrors the production
tombstone/idempotency semantics so these run without PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from research.participants.enums import EnrollmentStatus
from research.participants.models import Enrollment, RetentionJob, RetentionJobState
from research.participants.retention import execute_retention, run_retention_for_enrollment
from research.study.protocol.enums import RetentionAction
from research.telemetry.ingestion.models import ResearchEventRecord

from ._retention_fakes import InMemoryRetentionStore

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

TERMINAL_STATUSES = (EnrollmentStatus.STUDY_STOPPED, EnrollmentStatus.REVOKED)


def _enrollment(status: EnrollmentStatus) -> Enrollment:
    return Enrollment(
        enrollment_id=uuid.uuid4(),
        participant_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        participant_code="participant-code",
        status=status,
        eligibility={"eligible": True, "reasons": [], "evaluated_at": NOW},
        enrolled_at=NOW,
        consent_accepted_at=NOW,
        updated_at=NOW,
    )


def _event(enrollment: Enrollment) -> ResearchEventRecord:
    return ResearchEventRecord(
        event_id=uuid.uuid4(),
        schema_version="1",
        event_type="tool.completed",
        source="ide",
        study_id=enrollment.study_id,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=uuid.uuid4(),
        emitter_id="emitter",
        emitter_sequence=1,
        occurred_at=NOW,
        envelope={
            "account_id": "private-account",
            "email": "private@example.com",
        },
        digest="digest",
        accepted_at=NOW,
    )


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_terminal_enrollments_remain_retainable_with_audit_evidence(status):
    enrollment = _enrollment(status)
    store = InMemoryRetentionStore()
    store.enrollment_study[enrollment.enrollment_id] = enrollment.study_id
    store.add_event(_event(enrollment))

    job = run_retention_for_enrollment(
        None,
        enrollment.enrollment_id,
        RetentionAction.RETAIN_ANONYMIZED,
        store_api=store,
        now=NOW,
    )

    assert job.state == RetentionJobState.COMPLETED, job.last_error
    assert job.evidence_digest
    assert store.evidence, "retention must preserve content-free audit evidence"


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_delete_all_retention_still_revokes_collection_for_terminal_enrollments(status):
    enrollment = _enrollment(status)
    store = InMemoryRetentionStore()
    store.enrollment_study[enrollment.enrollment_id] = enrollment.study_id
    store.add_event(_event(enrollment))
    live_session_id = uuid.uuid4()
    store.add_session(
        type(
            "SessionDouble",
            (),
            {
                "research_session_id": live_session_id,
                "enrollment_id": enrollment.enrollment_id,
            },
        )()
    )

    job = run_retention_for_enrollment(
        None,
        enrollment.enrollment_id,
        RetentionAction.DELETE_ALL,
        store_api=store,
        now=NOW,
    )

    assert job.state == RetentionJobState.COMPLETED, job.last_error
    assert live_session_id in store.revoked_sessions


def test_retention_execution_is_idempotent_for_a_terminal_enrollment():
    enrollment = _enrollment(EnrollmentStatus.STUDY_STOPPED)
    job = RetentionJob(
        job_id=uuid.uuid4(),
        enrollment_id=enrollment.enrollment_id,
        action=RetentionAction.RETAIN_ANONYMIZED,
        state=RetentionJobState.COMPLETED,
        attempts=1,
        created_at=NOW,
        completed_at=NOW,
        evidence_digest="digest",
    )
    store = InMemoryRetentionStore()

    execution = execute_retention(
        job,
        [_event(enrollment)],
        RetentionAction.RETAIN_ANONYMIZED,
        now=NOW,
        applier=store,
    )

    assert execution.reused is True
    assert execution.job.state == RetentionJobState.COMPLETED
