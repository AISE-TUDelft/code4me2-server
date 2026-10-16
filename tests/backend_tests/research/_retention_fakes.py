"""Test-only in-memory retention store double.

``InMemoryRetentionStore`` enforces the same idempotency and tombstone
semantics as the production SQLAlchemy adapter so the retention worker is
machine-testable without PostgreSQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Sequence

from research.participants.enums import RetentionJobState, RetentionState

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from research.participants.models import (
        RetentionEvidence,
        RetentionJob,
    )
    from research.telemetry.ingestion.models import ResearchEventRecord

_LINKABLE_KEYS = ("account_id", "email", "session_token")


def _strip_linkable(mapping: Any) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    return {key: value for key, value in mapping.items() if key not in _LINKABLE_KEYS}


class InMemoryRetentionStore:
    """Deterministic in-memory retention store for tests."""

    def __init__(self) -> None:
        self.events: dict[UUID, ResearchEventRecord] = {}
        self.jobs: dict[UUID, RetentionJob] = {}
        self.evidence: list[RetentionEvidence] = []
        self.enrollment_study: dict[UUID, UUID] = {}
        self.sessions: dict[UUID, Any] = {}
        self.revoked_sessions: set[UUID] = set()
        self.fail_events = False

    # -- enrollment / events ---------------------------------------------

    def add_event(self, record: ResearchEventRecord) -> None:
        """Register an event (test setup helper)."""
        self.events[record.event_id] = record

    def get_enrollment_study_id(self, enrollment_id: UUID) -> Optional[UUID]:
        return self.enrollment_study.get(enrollment_id)

    def load_events(self, enrollment_id: UUID) -> list[ResearchEventRecord]:
        return [
            record
            for record in self.events.values()
            if record.enrollment_id == enrollment_id
            and record.retention_state != RetentionState.DELETED.value
        ]

    def mark_events_deleted(
        self, event_ids: Sequence[UUID], now: datetime
    ) -> int:
        if self.fail_events:
            raise RuntimeError("in-memory retention store: event failure")
        count = 0
        for event_id in event_ids:
            record = self.events.get(event_id)
            if record is None:
                continue
            self.events[event_id] = record.model_copy(
                update={
                    "retention_state": RetentionState.DELETED.value,
                    "enrollment_id": None,
                    "research_session_id": None,
                    "agent_run_id": None,
                    "envelope": {},
                }
            )
            count += 1
        return count

    def anonymize_events(
        self, event_ids: Sequence[UUID], now: datetime
    ) -> int:
        if self.fail_events:
            raise RuntimeError("in-memory retention store: event failure")
        count = 0
        for event_id in event_ids:
            record = self.events.get(event_id)
            if record is None:
                continue
            envelope = dict(record.envelope or {})
            for key in ("payload", "provenance"):
                if isinstance(envelope.get(key), dict):
                    envelope[key] = _strip_linkable(envelope[key])
            self.events[event_id] = record.model_copy(
                update={
                    "retention_state": RetentionState.ANONYMIZED.value,
                    "envelope": envelope,
                }
            )
            count += 1
        return count

    # -- sessions ---------------------------------------------------------

    def add_session(self, session: Any) -> None:
        """Register a session double (test setup helper)."""
        self.sessions[session.research_session_id] = session

    def revoke_sessions(self, enrollment_id: UUID, now: datetime) -> int:
        count = 0
        for session_id, session in self.sessions.items():
            if getattr(session, "enrollment_id", None) == enrollment_id:
                self.revoked_sessions.add(session_id)
                count += 1
        return count

    # -- jobs -------------------------------------------------------------

    def get_job(self, job_id: UUID) -> Optional[RetentionJob]:
        return self.jobs.get(job_id)

    def get_job_for_enrollment_action(
        self, enrollment_id: UUID, action: Any
    ) -> Optional[RetentionJob]:
        for job in self.jobs.values():
            if job.enrollment_id == enrollment_id and job.action == action:
                return job
        return None

    def upsert_job(self, job: RetentionJob) -> RetentionJob:
        for existing in self.jobs.values():
            if (
                existing.job_id != job.job_id
                and existing.enrollment_id == job.enrollment_id
                and existing.action == job.action
            ):
                return existing
        self.jobs[job.job_id] = job
        return job

    def list_pending_jobs(self, limit: int) -> list[RetentionJob]:
        pending = sorted(
            (
                job
                for job in self.jobs.values()
                if job.state
                in (RetentionJobState.PENDING, RetentionJobState.RETRYABLE)
            ),
            key=lambda job: job.created_at,
        )
        return pending[:limit]

    def insert_evidence(self, evidence: RetentionEvidence) -> None:
        self.evidence.append(evidence)
